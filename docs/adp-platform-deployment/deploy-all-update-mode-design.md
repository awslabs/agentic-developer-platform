# Design Note: deploy-all.sh Update Mode (Issue #3414)

Extends `deploy-all-update-design.md` (#3408) with explicit **update semantics**
so `deploy-all.sh` can safely converge an already-deployed platform — not just
stand up a fresh account or re-run idempotently.

**Author:** @agent-architect
**Date:** 2026-07-09
**Status:** Design complete — pending review
**Parent:** EPIC #3312; extends #3408 design; references #3410, #3413, #2909

---

## 0. Problem Statement

The #3408 rework makes `deploy-all.sh` idempotent (safe to *re-run*), but
re-run ≠ update. Three failure modes exist today when re-running on a live
deployment after pulling newer code:

| Failure mode | Root cause | Blast radius |
|---|---|---|
| Silent backend no-op | Image tag is `:latest`; if the ECR tag already existed (same digest), `kubectl set image` is a no-op — no rollout triggers | Operator believes backend updated; old code keeps running |
| Skipped migrations | deploy-all.sh has zero alembic logic; the only migration path is manual (`run-gateway-migrations.yml` or `kubectl exec`) | Backend crashes against stale schema after a migration-bearing update |
| Destructive Terraform drift | `-auto-approve` runs unconditionally; known landmines (agent-context Neptune #2769, gateway kms:Decrypt #2909) can plan destroys from drift | Resources destroyed on a live platform |

This design adds an **`--update` mode** that addresses all three.

---

## 1. Mode Semantics

### Decision: explicit `--update` flag (not auto-detect)

**Flag:** `--update`

**Behavior difference from default (fresh-deploy) mode:**

| Aspect | Default (fresh deploy) | `--update` mode |
|--------|------------------------|-----------------|
| Bootstrap (S3/DynamoDB) | Runs (creates if missing) | Skips — precondition: state bucket must exist |
| Bedrock model agreements | Runs | Skips (idempotent but slow; not needed on update) |
| Terraform applies | `-auto-approve` unconditionally | Plan-first; refuse if destroys detected (see §4) |
| Image build | Always (`:latest` tag) | Always — but forces rollout (see §2) |
| Database migrations | Not run | Runs alembic before backend rollout (see §3) |
| Admin bootstrap | Runs (idempotent) | Skips — admin already exists on a live platform |
| GitHub App prompt (summary) | Shown | Not shown — already configured |
| Preflight | Full | Partial (skip tool-install checks; keep AWS auth + cluster-reachable) |

**Precondition checks (fail-fast in `--update` mode):**

```bash
# 1. State bucket must exist
aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null \
  || fail "--update requires an existing deployment. State bucket '$STATE_BUCKET' not found."

# 2. EKS cluster must exist and be ACTIVE
CLUSTER_STATUS=$(aws eks describe-cluster --name "$EKS_CLUSTER" \
  --query 'cluster.status' --output text 2>/dev/null) || true
[ "$CLUSTER_STATUS" = "ACTIVE" ] \
  || fail "--update requires a running EKS cluster. Got status: ${CLUSTER_STATUS:-not found}"

# 3. Gateway namespace must exist (indicates prior deploy)
kubectl get namespace adp-gateway &>/dev/null \
  || fail "--update requires prior gateway deployment. Namespace 'adp-gateway' not found."
```

**Rationale for explicit flag (not auto-detect):**
- Auto-detect (state bucket exists → update mode) is dangerous: a partially-failed
  fresh deploy leaves a state bucket but NOT a working platform — auto-detecting
  "update" mode would skip bootstrap recovery steps.
- Explicit flag makes the operator's intent unambiguous. The precondition checks
  catch misuse (running `--update` on a fresh account).
- Mirrors the pipeline track where "apply" vs "plan" is a conscious choice.

**Flag composition:**

| Existing flag | Compatible with `--update`? | Notes |
|---|---|---|
| `--gateway-only` | Yes | Updates only gateway-scoped resources |
| `--agent-factory-only` | Yes | Updates only agent-factory-scoped resources |
| `--agent-context-only` | Yes | Updates only agent-context-scoped resources |
| `--skip-frontend` | Yes | Skips frontend rebuild (useful when only backend changed) |
| `--local` | Yes | Uses local Docker instead of CodeBuild |
| `--destroy` | No — mutually exclusive | Fail: `--update and --destroy are mutually exclusive` |
| `--ci` | No — mutually exclusive | Fail: `--update and --ci are mutually exclusive` |

---

## 2. Image Rollout Correctness

### Problem today

`deploy-all.sh` (line 658):
```bash
kubectl set image deployment/bedrockgateway \
  bedrockgateway="${REGISTRY}/adp-gateway:latest" -n adp-gateway 2>/dev/null || true
```

If the ECR `:latest` tag still points to the same digest (because CodeBuild
pushed the same content), Kubernetes sees no image change → no rollout.

The CI track (gateway-deploy.yml, line 302) avoids this by tagging with
`${{ github.sha }}`:
```bash
kubectl set image deployment/bedrockgateway \
  bedrockgateway=${REGISTRY}/adp-gateway:${{ github.sha }} -n ${{ env.NAMESPACE }}
```

### Decision: SHA-tagged images + forced rollout verification

In `--update` mode, the script:

1. **Tags images with the source SHA** (not just `:latest`):
   ```bash
   # Determine source SHA for tagging
   SOURCE_SHA=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "manual-$(date +%s)")
   export IMAGE_TAG="$SOURCE_SHA"
   ```
   The `IMAGE_TAG` env var is already respected by `codebuild-run.sh` → buildspec
   (`bs-gateway-build.yml` line 25: `[ -n "${IMAGE_TAG:-}" ] && docker tag ...`).
   Local mode (`--local`) applies the same tag.

2. **Sets the image with the SHA tag** (guarantees Kubernetes sees a change):
   ```bash
   kubectl set image deployment/bedrockgateway \
     bedrockgateway="${REGISTRY}/adp-gateway:${IMAGE_TAG}" -n adp-gateway
   ```

3. **Verifies rollout completion** (already present but now mandatory — no
   `2>/dev/null || true` suppression):
   ```bash
   kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s \
     || fail "Gateway rollout failed. Check: kubectl describe deployment/bedrockgateway -n adp-gateway"
   ```

4. **Post-rollout health check** (new):
   ```bash
   # Wait for new pods to pass readiness, then verify /health
   sleep 5
   HEALTH=$(kubectl exec -n adp-gateway deploy/bedrockgateway -- \
     curl -sf http://localhost:8080/health 2>/dev/null) || true
   echo "$HEALTH" | grep -q '"status"' \
     || warn "Health check inconclusive. Verify: kubectl exec -n adp-gateway deploy/bedrockgateway -- curl http://localhost:8080/health"
   ```

**Applies to all images:**
- `adp-gateway` (gateway backend)
- `adp-agent-gateway` (agent gateway)
- `adp-agent-runtime` (webhook-ingress worker)

**Fresh-deploy mode (no `--update`):** unchanged — keeps `:latest` tag behavior
(acceptable for initial deploy where there's no running workload to disrupt).

---

## 3. Database Migrations

### Problem today

`deploy-all.sh` has **zero** migration logic. The only path is:
- `run-gateway-migrations.yml` (manual workflow_dispatch or called by `gateway-deploy.yml`)
- Direct `kubectl exec` into a running pod

On an update, a code change that includes a new alembic revision will make the
gateway pod crash on startup if the schema is behind (FastAPI's `create_all`
doesn't run alembic — it only creates tables missing from metadata, which doesn't
help when a column is added to an existing table).

### Decision: migrations run BEFORE backend image rollout

**Ordering:**
```
[gateway-infra terraform] → [migrations] → [image build + rollout] → [verify]
```

**Rationale:** Alembic migrations must be backward-compatible (the existing
gateway-deploy.yml workflow runs migrations *before* the image update — line
461-475). The old code must tolerate the new schema. This is the standard
"expand-then-contract" pattern.

**Implementation in `--update` mode:**

```bash
# ─── Migrations (update mode only) ───
if [ "$UPDATE_MODE" = true ]; then
  step "Step N/M: Run database migrations"

  # Find a running gateway pod (mirrors run-gateway-migrations.yml logic)
  NS="adp-gateway"
  POD=$(kubectl get pods -n "$NS" --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true

  if [ -z "$POD" ]; then
    warn "No running gateway pod found. Skipping migrations (will run after rollout if pods start)."
  else
    echo "Running alembic migrations on $NS/$POD..."
    echo "=== Current revision ==="
    kubectl exec -n "$NS" "$POD" -- env PYTHONPATH=/app alembic current 2>&1 || true
    echo ""
    echo "=== Upgrading to head ==="
    kubectl exec -n "$NS" "$POD" -- env PYTHONPATH=/app alembic upgrade head \
      || fail "Alembic migration failed. Check: kubectl exec -n $NS $POD -- env PYTHONPATH=/app alembic history"
    echo ""
    echo "=== New revision ==="
    kubectl exec -n "$NS" "$POD" -- env PYTHONPATH=/app alembic current
    ok "Migrations complete"
  fi
fi
```

**Failure handling:**
- If `alembic upgrade head` fails, the script halts (`set -e` + explicit `fail`).
  The operator must resolve the migration error before proceeding.
- The old pods remain running on the old image (no rollout yet), so the platform
  stays functional on the old schema revision (assuming the migration is
  backward-compatible — if not, this is a schema design bug, not a deploy bug).

**No-migration case:** If `alembic current` already shows HEAD, `alembic upgrade
head` is a no-op (exits 0 instantly). No harm.

**Agent-context migrations:** Agent-context has its own schema (Neptune, DynamoDB).
Neptune schema changes are handled by the SCIP ingestion pipeline (additive graph
writes, not DDL). DynamoDB tables are Terraform-managed. No separate migration step
needed for agent-context — Terraform handles it.

---

## 4. Terraform Destroy Gate

### Problem today

Every `terraform apply` in `deploy-all.sh` runs with `-auto-approve`. On a live
deployment with drift, this can plan destroys and execute them without warning.

**Known landmines (from operational experience):**

| Module | Drift source | Planned action | Impact if auto-approved |
|--------|---|---|---|
| agent-context | Neptune cluster feature flags changed externally or TF state stale | `9 resources will be destroyed` (full Neptune cluster + subnet group + IAM) | Total loss of code graph data |
| gateway | Missing `moved` block for kms:Decrypt policy (#2909) | `1 resource will be destroyed, 1 will be created` (destroy+recreate IAM policy) | Brief period where agent pods lose decrypt permission → CrashLoopBackOff |
| gateway | identity-index DynamoDB rename (#537) | Potential destroy if someone adds a `moved` block without coordination | Loss of channel-tenant cache → GitHub login breaks |
| platform | EKS access entry drift (role ARN format changes between CI and human applies) | Access entry destroy + recreate | Brief loss of cluster access for the affected role |

### Decision: plan-first gate mirroring CI workflow pattern

In `--update` mode, every `terraform apply` is replaced with:

```bash
terraform_update_apply() {
  local MODULE_NAME="$1"
  local VAR_FILE="$2"
  shift 2
  local EXTRA_VARS=("$@")

  # 1. Plan to a file (captures the plan for inspection)
  local PLAN_FILE="/tmp/tf-plan-${MODULE_NAME}-$$.tfplan"
  local PLAN_OUTPUT="/tmp/tf-plan-${MODULE_NAME}-$$.txt"

  terraform plan \
    -var-file="$VAR_FILE" \
    "${EXTRA_VARS[@]}" \
    -out="$PLAN_FILE" \
    -detailed-exitcode 2>&1 | tee "$PLAN_OUTPUT"
  local EXIT_CODE=${PIPESTATUS[0]}

  # Exit code: 0 = no changes, 1 = error, 2 = changes present
  if [ "$EXIT_CODE" -eq 0 ]; then
    ok "$MODULE_NAME: no changes"
    return 0
  elif [ "$EXIT_CODE" -eq 1 ]; then
    fail "$MODULE_NAME: terraform plan failed"
  fi

  # 2. Check for destroys
  DESTROYS=$(grep -c 'will be destroyed' "$PLAN_OUTPUT" 2>/dev/null) || DESTROYS=0

  if [ "$DESTROYS" -gt 0 ]; then
    echo ""
    echo -e "${RED}━━━ DESTROY GATE ━━━${NC}"
    echo -e "${RED}$MODULE_NAME plan includes $DESTROYS resource(s) to be destroyed:${NC}"
    echo ""
    grep 'will be destroyed' "$PLAN_OUTPUT"
    echo ""

    if [ "$CONFIRM_DESTRUCTIVE" = true ]; then
      warn "Operator confirmed destructive apply (--confirm-destructive). Proceeding."
    else
      echo "To authorize this apply, re-run with --confirm-destructive."
      echo "To inspect the full plan: cat $PLAN_OUTPUT"
      echo ""
      echo "Known drift issues to check:"
      echo "  - agent-context/Neptune: full apply may want to destroy 9 Neptune resources (Issue #2769)"
      echo "  - gateway/kms:Decrypt policy: missing 'moved' block causes destroy+recreate (Issue #2909)"
      echo "  - platform/EKS access entries: role ARN format drift between CI and manual applies"
      echo ""
      fail "Refusing to apply $MODULE_NAME plan with $DESTROYS destroy(s). Pass --confirm-destructive to override."
    fi
  fi

  # 3. Apply the saved plan (no -auto-approve needed — plan file is pre-approved)
  terraform apply "$PLAN_FILE"
  ok "$MODULE_NAME: applied successfully"

  # Cleanup
  rm -f "$PLAN_FILE" "$PLAN_OUTPUT"
}
```

**New flag:** `--confirm-destructive`

| Flag | Purpose | Default |
|------|---------|---------|
| `--confirm-destructive` | Authorize Terraform applies that include resource destroys | off |

Mirrors `confirm_destructive_apply` in `platform-infra-apply.yml` (line 33) and
`gateway-infra-apply.yml` (line 30). Same semantic: "I have reviewed the plan and
accept the destroys."

**Fresh-deploy mode (no `--update`):** unchanged — keeps `-auto-approve` (on a fresh
account there are no existing resources to accidentally destroy; plan will only show
creates).

**Gate output example:**
```
━━━ DESTROY GATE ━━━
gateway plan includes 1 resource(s) to be destroyed:

  # module.gateway_kms.aws_iam_policy.kms_decrypt will be destroyed

To authorize this apply, re-run with --confirm-destructive.
To inspect the full plan: cat /tmp/tf-plan-gateway-12345.txt

Known drift issues to check:
  - agent-context/Neptune: full apply may want to destroy 9 Neptune resources (Issue #2769)
  - gateway/kms:Decrypt policy: missing 'moved' block causes destroy+recreate (Issue #2909)
  - platform/EKS access entries: role ARN format drift between CI and manual applies

✗ Refusing to apply gateway plan with 1 destroy(s). Pass --confirm-destructive to override.
```

### Destroy-gate scope

The gate applies to ALL terraform applies in update mode:
- Platform infra (`platform/infra/`)
- Gateway infra (`modules/gateway/infra/`)
- Gateway second pass (ALB wiring re-apply)
- Agent-factory infra (`modules/agent-factory/infra/`)
- Webhook-ingress infra (`modules/agent-factory/webhook-ingress/infra/`)
- Agent-context infra (`modules/agent-context/terraform/`)

---

## 5. Selective Update

### Decision: v1 is whole-platform convergence; per-surface via existing scope flags

**Rationale:**
- The existing `--gateway-only`, `--agent-factory-only`, `--agent-context-only`
  flags already provide per-surface targeting.
- Adding a new `--update gateway` syntax would duplicate existing flag semantics
  and introduce a second way to scope.
- The destroy-gate (§4) is the protection layer: even if you run `--update`
  against the whole platform, you only apply modules where the plan is
  non-destructive.
- The no-changes fast-path (`terraform plan` exit code 0 → skip) means unchanged
  modules complete in seconds.

**Usage patterns:**

```bash
# Update only the gateway (code change + migration):
./platform/scripts/deploy-all.sh --update --gateway-only

# Update everything, but refuse destructive changes:
./platform/scripts/deploy-all.sh --update

# Update everything, accept known drift destruction:
./platform/scripts/deploy-all.sh --update --confirm-destructive

# Update gateway, skip frontend rebuild (backend-only change):
./platform/scripts/deploy-all.sh --update --gateway-only --skip-frontend
```

**Future consideration (out of scope for v1):** A `--plan-only` flag that runs
all terraform plans + shows the image diff + shows pending migrations, but applies
nothing. Useful for pre-flight validation before committing to an update. This is
analogous to the CI plan workflows and could be added as a follow-on.

---

## 6. Relationship to Tracks

### Self-managed / customer-install (this design)

`deploy-all.sh --update` is for operators who:
- Run ADP in their own AWS account (self-managed track)
- Pull newer `main` (or a pinned tag) and want to converge their deployment
- Are the #3312 environment accounts (inttest/systest/preprod) converging from a newer tag
- Do NOT have CI pipelines wired up for the ADP repo

These operators use the script as their single deployment tool.

### ADP platform account (NOT this design)

The ADP-managed platform account (`aws-e/adp` repo) uses per-surface CI pipelines:
- `gateway-deploy.yml` — image build + K8s rollout + migrations (on push to main)
- `platform-infra-apply.yml` — TF apply with destroy-gate (manual dispatch)
- `gateway-infra-apply.yml` — TF apply with destroy-gate (manual dispatch)
- `agent-context-deploy.yml` — deploy on push to module path
- `webhook-ingress-deploy.yml` — deploy on push to module path

`deploy-all.sh --update` does NOT replace these pipelines. The pipelines provide:
- PR-gated review (plan on PR, apply on merge/dispatch)
- Per-surface granularity (only the changed module deploys)
- Audit trail (GitHub Actions logs)
- Concurrency control (workflow-level mutex)

**State it explicitly:** This design targets **self-managed operators and #3312 env
accounts only**. The ADP platform account continues using its CI pipelines for all
updates. `deploy-all.sh --update` is the self-managed equivalent of "merge a PR and
the pipeline handles it."

---

## 7. Update Mode Phase Sequence

Building on the #3408 target (11-step structure), here is the update-mode flow:

```
Step  1/11: Bootstrap                    → SKIPPED (precondition-checked)
Step  2/11: Platform infra               → plan-gated apply
Step  3/11: Gateway infra                → plan-gated apply
Step  4/11: Gateway backend:
            4a: Migrations               → alembic upgrade head (NEW)
            4b: Image build              → SHA-tagged (not :latest)
            4c: Image rollout            → kubectl set image + rollout status
            4d: Health check             → curl /health (NEW)
Step  5/11: Wire internal ALB            → plan-gated re-apply (only if ALB vars changed)
Step  6/11: Frontend                     → rebuild + S3 sync + CF invalidation
Step  7/11: Broker Lambda                → re-package + update-function-code (idempotent)
Step  8/11: Admin bootstrap              → SKIPPED (admin already exists)
Step  9/11: Agent-factory:
            9a: Infra                    → plan-gated apply
            9b: Agent-gateway image      → SHA-tagged build + rollout
Step 10/11: Webhook-ingress:
            10a: Agent-runtime image     → SHA-tagged build
            10b: Webhook Lambda          → re-package + upload
            10c: Infra                   → plan-gated apply
Step 11/11: Agent-context (if enabled)   → plan-gated apply + redeploy
```

**Steps skipped in update mode:**
- Step 1 (bootstrap) — state backend already exists
- Step 8 (admin bootstrap) — admin already provisioned
- Bedrock model agreements — already accepted (idempotent but unnecessary)

**Steps that gain new behavior in update mode:**
- All terraform steps — plan-gated (refuse destroys without `--confirm-destructive`)
- Gateway backend — migrations before rollout; SHA-tagged image; health check
- Agent-gateway — SHA-tagged image
- Agent-runtime — SHA-tagged image

---

## 8. No-Change Fast Path (Convergence)

When an operator runs `--update` and nothing has changed:

1. **Terraform modules:** `terraform plan` exit code 0 → "no changes" → skip apply.
   Runs in seconds per module (reads state, compares, exits).
2. **Image build:** CodeBuild runs regardless (determines if content changed
   internally). If the SHA tag already exists in ECR, the push is a no-op.
   However, `kubectl set image` with the same tag is a no-op too — so we force
   a rollout restart if the image digest matches:
   ```bash
   CURRENT_IMAGE=$(kubectl get deployment/bedrockgateway -n adp-gateway \
     -o jsonpath='{.spec.template.spec.containers[0].image}')
   if [ "$CURRENT_IMAGE" = "${REGISTRY}/adp-gateway:${IMAGE_TAG}" ]; then
     echo "Image tag unchanged. Checking if digest changed..."
     kubectl rollout restart deployment/bedrockgateway -n adp-gateway
   else
     kubectl set image deployment/bedrockgateway \
       bedrockgateway="${REGISTRY}/adp-gateway:${IMAGE_TAG}" -n adp-gateway
   fi
   ```
3. **Migrations:** `alembic upgrade head` on HEAD revision is a no-op (exits 0).
4. **Frontend:** `aws s3 sync` only uploads changed files. CloudFront
   invalidation runs regardless (cheap, ensures no stale cache).
5. **Broker Lambda:** `update-function-code` with identical zip is accepted by
   AWS (no-op from Lambda's perspective).

**Expected wall-clock for no-change convergence:** ~3-5 minutes (dominated by
terraform plan executions across 4-6 modules × ~30s each).

---

## 9. Error Handling and Rollback

### On failure in update mode

| Failure point | State | Recovery |
|---|---|---|
| Terraform plan fails | No changes applied | Fix the TF error, re-run |
| Destroy gate blocks | No changes applied | Inspect plan, fix drift OR pass `--confirm-destructive` |
| Migration fails | Old code + old schema running | Fix migration, re-run. Platform stays on old version. |
| Image build fails | Old code running | Fix build error, re-run. Platform stays on old version. |
| Rollout fails (pod crash) | Old pods terminating, new pods in CrashLoopBackOff | `kubectl rollout undo deployment/bedrockgateway -n adp-gateway`. Investigate pod logs. |
| Health check fails (post-rollout) | New pods running but unhealthy | Script warns (non-fatal). Operator investigates. |
| Frontend deploy fails | Old frontend served by CloudFront | Fix build, re-run. CDN cache serves stale until next successful invalidation. |

### Rollback is NOT automated

`deploy-all.sh --update` does not provide automatic rollback. Rationale:
- Terraform rollback = previous apply (which may itself be destructive)
- Image rollback = `kubectl rollout undo` (simple but requires knowing *which*
  deployment failed)
- Migration rollback = `alembic downgrade` (requires down-migration to exist,
  which is not guaranteed)

Instead: the script **halts on failure** (preserving the pre-failure state for
all subsequent steps) and the operator resolves manually. This matches the CI
track behavior (failed workflow → operator investigates, re-runs).

---

## 10. Implementation Flags Summary

New flags for `deploy-all.sh`:

| Flag | Purpose | Mutually exclusive with |
|------|---------|------------------------|
| `--update` | Enable update mode (plan-gate, migrations, SHA-tags, skip bootstrap/admin) | `--destroy`, `--ci` |
| `--confirm-destructive` | Authorize terraform applies containing resource destroys (only meaningful with `--update`) | — |

Updated `--help` output:
```
Usage: deploy-all.sh [OPTIONS]

Modes:
  (default)              Fresh deploy — stand up the platform from scratch
  --update               Update mode — converge an existing deployment to newer code
  --destroy              Tear down all infrastructure
  --ci                   CI mode: validate outputs exist without re-applying

Update-mode flags (only with --update):
  --confirm-destructive  Authorize terraform applies that include resource destroys

Scope:
  --gateway-only         Platform + gateway only
  --agent-factory-only   Platform + agent-factory only
  --agent-context-only   Platform + agent-context only

Skip:
  --skip-frontend        Skip frontend build and deploy
  --skip-broker          Skip broker Lambda deploy
  --skip-admin-bootstrap Skip first-admin DB seeding
  --skip-webhook-ingress Skip webhook-ingress stack
  --skip-agent-context   Skip agent-context even if enabled

Build:
  --local                Use local Docker for image builds (instead of CodeBuild)
```

---

## 11. Known Drift Landmines Registry

Documented here so the destroy-gate's help text can reference them. Operators
hitting the gate should check this list first.

| ID | Module | Resource | Symptom | Root cause | Fix |
|---|---|---|---|---|---|
| DRIFT-1 | agent-context | `module.neptune_serverless[0].*` (9 resources) | Full Neptune destroy+recreate | State file references Neptune resources but `neptune_enabled` toggled or subnet selection changed | Set `neptune_enabled=true` in tfvars; or `terraform state rm` the Neptune resources if intentionally disabling |
| DRIFT-2 | gateway | `module.gateway_kms.aws_iam_policy.kms_decrypt` | Destroy + recreate (brief permission gap) | Issue #2909 — resource was refactored without a `moved` block | Add a `moved { from = ...; to = ... }` block in gateway infra, or pass `--confirm-destructive` (self-heals on recreate) |
| DRIFT-3 | platform | `aws_eks_access_entry` | Destroy + recreate | Role ARN format differs between CI runs (assumed-role) and manual applies (IAM role) | Fixed in current main (ARN normalization in platform/infra/main.tf). If seen, pull latest. |
| DRIFT-4 | gateway | `aws_dynamodb_table.identity_index` | Potential rename destroy | Future `moved` block (#537) not yet applied | Do NOT rename this resource without a `moved` block. Tracked in issue #537. |

---

## 12. Relationship to Existing Scripts

`deploy-all.sh --update` reuses existing scripts without modification:

| Script | Called in update mode? | Behavior difference |
|--------|:---------------------:|---------------------|
| `preflight-check.sh` | Yes (partial) | Skip tool-install checks; keep auth + cluster checks |
| `wire-gateway-alb.sh` | Yes | No change — SSM cache hit means fast no-op |
| `deploy-broker.sh` | Yes | No change — idempotent re-package + update |
| `deploy-webhook-ingress.sh` | Yes | `IMAGE_TAG` env var flows through for SHA-tagged build |
| `deploy-frontend.sh` | No (inline logic) | No change — S3 sync is inherently incremental |
| `bootstrap-admin.sh` | No — skipped | Admin already exists |
| `codebuild-run.sh` | Yes | `IMAGE_TAG` env var already supported |

**No new scripts are created.** The update-mode logic lives entirely in
`deploy-all.sh` as conditional branches gated on `UPDATE_MODE=true`.

---

## 13. Verdict

Design ready for implementation. The key decisions are:

1. **Explicit `--update` flag** — not auto-detect (safety over convenience)
2. **SHA-tagged images** — guarantees rollout triggers on code change
3. **Migrations before rollout** — expand-then-contract; mirrors CI track
4. **Plan-first destroy gate** — mirrors `confirm_destructive_apply` from CI workflows
5. **Whole-platform convergence** — existing scope flags (`--gateway-only` etc.) provide per-surface targeting
6. **Self-managed track only** — ADP platform account stays on CI pipelines

Implementation scope (follow-on issue):
- Add `--update` and `--confirm-destructive` flag parsing
- Add precondition checks (state bucket, EKS, namespace)
- Add `terraform_update_apply()` helper function
- Add migration step (between gateway-infra and gateway-rollout)
- Switch image tagging to SHA in update mode
- Add rollout verification + health check
- Skip bootstrap + admin-bootstrap + bedrock-agreements in update mode
- Update `--help` text
- Update cross-references in `self-managed-deploy.md` and `deploy-quickstart.md`
