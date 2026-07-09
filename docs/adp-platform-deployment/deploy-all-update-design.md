# Design Note: deploy-all.sh Update (Issue #3408)

Gap analysis and target design for updating `platform/scripts/deploy-all.sh` to
cover the full self-managed installation sequence.

**Author:** @agent-architect
**Date:** 2026-07-09
**Status:** Design complete — ready for implementation review
**Parent:** EPIC #3312; references deploy instance #2588, EPIC #2571

---

## 1. Phase Mapping — Current vs. Canonical

The canonical self-managed sequence (from `deploy-quickstart.md` Phase 1–10,
verified end-to-end) maps to `deploy-all.sh` as follows:

| Canonical Phase | What | Script (standalone) | deploy-all.sh today | Gap |
|:-:|---|---|---|---|
| 1 | Bootstrap (S3 + DynamoDB) | `platform/scripts/bootstrap.sh` | ✅ Step 1/8 — inline logic (not calling bootstrap.sh) | Minor: doesn't call the canonical script; duplicates logic. Acceptable — existing behavior, no change needed. |
| 2 | Preflight | `platform/scripts/preflight-check.sh` | ✅ Preflight section (calls the script) | None |
| — | Bedrock model agreements | `platform/scripts/enable-bedrock-models.sh` | ✅ Runs after preflight | None |
| 3 | Platform infra | terraform in `platform/infra/` | ✅ Step 2/8 | None |
| 4 | Gateway infra | terraform in `modules/gateway/infra/` | ✅ Step 3/8 | None |
| 5 | Gateway backend (image + K8s) | CodeBuild + kubectl | ✅ Step 4/8 | None |
| 6 | Frontend (React → S3 → CF) | `modules/gateway/scripts/deploy-frontend.sh` | ✅ Step 5/8 — inline (not calling the script) | Minor: uses inline npm/s3-sync instead of calling `deploy-frontend.sh`. Acceptable — existing pattern. |
| 6b | Wire ALB (gateway second pass) | `platform/scripts/wire-gateway-alb.sh --apply` | ✅ Step 4b/8 (calls the script, no `--apply` but does the re-apply inline) | None |
| **6c** | **Broker Lambda code** | `modules/gateway/scripts/deploy-broker.sh` | ❌ **MISSING** | **Critical gap** — without it, GitHub login returns 503 |
| **6d** | **First-admin bootstrap** | `modules/gateway/scripts/bootstrap-admin.sh` | ❌ **MISSING** | **Critical gap** — without it, no user can log in (all get "request access", no approver) |
| **7** | **Webhook-ingress stack** | `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh` | ❌ **MISSING** | **Gap** — the webhook agent path is dead without it |
| 8a | Bedrock model agreements | `platform/scripts/enable-bedrock-models.sh` | ✅ Already runs early (fine — order doesn't matter, idempotent) | None |
| **8b/9** | **GitHub App wiring** | UI flow or `register-github-app.sh` | ❌ **MISSING** (has interactive browser step) | **Gap** — requires human interaction; script must handle gracefully |

### Summary of Gaps

4 phases are absent from `deploy-all.sh`:

1. **Phase 6c — deploy-broker.sh** (gateway scope; required for login)
2. **Phase 6d — bootstrap-admin.sh** (gateway scope; required for first admin)
3. **Phase 7 — deploy-webhook-ingress.sh** (agent-factory scope; required for agent path)
4. **Phase 8b — register-github-app.sh** (agent-factory scope; has interactive browser step)

Additionally, the step numbering in `deploy-all.sh` is inconsistent (some steps
say `/6`, some say `/8`) — the script should be renumbered to reflect the actual
step count after the additions.

---

## 2. Target Phase List + Step Numbering

After the update, `deploy-all.sh` will have this flow:

```
Step  1/11: Bootstrap Terraform state backend        (unchanged)
Step  2/11: Deploy shared platform                   (unchanged)
Step  3/11: Deploy gateway infrastructure            (unchanged)
Step  4/11: Build and deploy gateway backend         (unchanged)
Step  5/11: Wire internal ALB to API Gateway         (was 4b/8 — same logic)
Step  6/11: Deploy frontend                          (unchanged)
Step  7/11: Deploy broker Lambda code                (NEW — calls deploy-broker.sh)
Step  8/11: Bootstrap first admin                    (NEW — calls bootstrap-admin.sh)
Step  9/11: Deploy agent-factory                     (unchanged — existing agent-factory + agent-gateway)
Step 10/11: Deploy webhook-ingress stack             (NEW — calls deploy-webhook-ingress.sh)
Step 11/11: Agent Context                            (unchanged — optional, gated)
```

**Phase 8b (GitHub App wiring) is intentionally NOT added as a step.** It
requires a human browser interaction (manifest flow) and cannot run
non-interactively in the general case. Instead:
- At the end of Step 10 (or the script summary), emit a clear prompt:
  *"To complete the agent path: log in as platform_admin → Settings →
  Connections → 'Set up GitHub App', or run register-github-app.sh <org>"*
- A new `--skip-github-app` flag is NOT needed (it's already excluded).
- If needed in the future, adding `--github-app-org <org>` could invoke
  `register-github-app.sh` non-interactively (with `--app-id` / `--pem-path`
  pre-provided) — but that's out of scope for this story.

---

## 3. Flag Semantics

### Existing flags — scope gating for new steps

| Flag | Steps skipped | New steps behavior |
|------|---|---|
| `--gateway-only` | Agent-factory (9), webhook-ingress (10), agent-context (11) | Broker (7) ✅ runs, admin (8) ✅ runs — both are gateway-scope |
| `--agent-factory-only` | Gateway (3-8), agent-context (11) | Broker ❌ skipped, admin ❌ skipped, webhook-ingress (10) ✅ runs |
| `--agent-context-only` | Gateway (3-8), agent-factory (9-10) | Broker ❌ skipped, admin ❌ skipped, webhook-ingress ❌ skipped |
| `--skip-frontend` | Frontend (6) only | No impact on new steps |

### New flags

| Flag | Purpose | Default | Behavior |
|------|---------|---------|----------|
| `--skip-broker` | Skip broker Lambda deploy | off | Skips step 7. Use when `enable_github_auth_broker=false` in tfvars |
| `--skip-admin-bootstrap` | Skip first-admin DB seeding | off | Skips step 8. Use on re-deploys where the admin already exists |
| `--skip-webhook-ingress` | Skip webhook agent stack | off | Skips step 10. Use when only the gateway is needed (alternative to `--gateway-only` when agent-factory infra is wanted but not the webhook path) |

**Rationale for skip flags (not enable flags):** These steps are idempotent and
should run by default in a full deploy. An operator who doesn't need a specific
step (e.g., re-deploying and the admin already exists) can skip it. This follows
the existing `--skip-frontend` pattern.

### Idempotency contracts

| New step | Idempotent? | Re-run behavior |
|----------|:-----------:|-----------------|
| deploy-broker.sh | ✅ | Re-packages and re-uploads; `update-function-code` overwrites (no-op if code unchanged) |
| bootstrap-admin.sh | ✅ | `approve_request` in the gateway pod is idempotent — checks for existing rows before insert |
| deploy-webhook-ingress.sh | ✅ | CodeBuild rebuild (idempotent ECR push); Lambda zip re-upload; `terraform apply` is idempotent |

All three scripts already support `--dry-run`. The deploy-all.sh caller does NOT
pass `--dry-run` (it's a deploy script, not a plan script); it calls them
straight.

---

## 4. Ordering Rationale

The phase order within `deploy-all.sh` is dictated by hard dependencies:

```
bootstrap(1) → platform(2) → gateway-infra(3) → gateway-backend(4) → ALB-wire(5)
                                                       ↓
                                                  frontend(6) → broker(7) → admin(8)
                                                       ↓
                                              agent-factory(9) → webhook-ingress(10)
                                                       ↓
                                              agent-context(11) [optional]
```

Key ordering constraints for new steps:
- **Broker (7) AFTER ALB-wire (5):** The broker Lambda needs the API Gateway
  routes to be live (real body, not MOCK). `deploy-broker.sh` itself only
  updates the Lambda code, but the route it serves (`/auth/github/*`) is only
  active after the ALB second pass.
- **Admin (8) AFTER gateway-backend (4):** Runs `kubectl exec` into the gateway
  pod — the pod must be healthy.
- **Admin (8) AFTER broker (7):** While not a hard technical dep, the admin
  bootstrap verifies login flow end-to-end; broker must be live first.
- **Webhook-ingress (10) AFTER platform (2):** Needs EKS + ECR + KEDA (all from
  platform/agent-factory infra). Also needs the agent-factory IAM roles (step 9).
- **Webhook-ingress (10) AFTER agent-factory (9):** The ScaledJob role, secrets,
  and namespace are provisioned by agent-factory terraform.

---

## 5. Destroy Symmetry

The `--destroy` path in `deploy-all.sh` currently destroys in order:
`agent-context → webhook-ingress → agent-factory → gateway → platform`

### What the destroy path already covers (no changes needed)

- **Webhook-ingress** (destroy step 2/5): already present — includes
  KEDA cleanup, Lambda artifact cleanup, terraform destroy.
- **Agent-factory** (destroy step 3/5): already present.
- **Gateway** (destroy step 4/5): already present — includes ALB, S3,
  Secrets, CloudFront, terraform destroy + SSM cleanup.

### What the destroy path needs

The new deploy steps (broker, admin, webhook-ingress) do NOT create
resources that need separate destroy logic:

| New deploy step | Creates what? | Destroyed by which existing destroy phase? |
|---|---|---|
| deploy-broker.sh | Updates Lambda code (Lambda itself is TF-managed) | Gateway destroy (step 4/5) destroys the Lambda via terraform |
| bootstrap-admin.sh | DB rows in RDS | Gateway destroy (step 4/5) destroys the RDS instance |
| deploy-webhook-ingress.sh | ECR image, Lambda zip in S3, webhook-ingress TF resources | Webhook-ingress destroy (step 2/5) already handles all of this |

**Conclusion:** No changes to the destroy path are required. Each new step
either modifies resources already owned by a terraform module (destroyed by
that module's `terraform destroy`) or writes data into a resource that gets
destroyed (RDS).

The one **caveat** is that `deploy-all.sh --destroy` is a **legacy path** —
the recommended teardown is `undeploy.sh`. The destroy path in `deploy-all.sh`
should remain stable without additions. A comment noting this (already present
at line 199-200) is sufficient.

---

## 6. Doc Reconciliation

### Issue claim: "`deploy-quickstart.md` no longer exists"

**This is incorrect.** `docs/adp-platform-deployment/deploy-quickstart.md` exists
and is the authoritative verified procedure (maintained against real end-to-end
runs). `CLAUDE.md` cites it as canonical. `self-managed-deploy.md` is the longer
reference doc. Both are valid and serve different purposes:
- `deploy-quickstart.md` — operator-facing "what broke and how to fix it" guide
- `self-managed-deploy.md` — comprehensive reference (prereqs, cost, CI/CD, etc.)

No doc retirement is needed.

### Changes needed in docs

| File | Change |
|------|--------|
| `deploy-all.sh` header comment | Update usage to list new `--skip-*` flags |
| `deploy-all.sh` `--help` output | Add new flags |
| `self-managed-deploy.md` § "Quick Start" / "What Gets Deployed" | Update the deployment tree diagram to show steps 7-10 (broker, admin, webhook-ingress). Currently lists steps 1-8 but notes "deploy-all.sh does NOT deploy this" for webhook path — that caveat must be removed/updated after implementation. |
| `deploy-quickstart.md` § "deploy-all.sh shortcut" (line ~854) | Update the note that says "chains Phases 1–6b. It does NOT do 6c/6d/7" to say "chains Phases 1–10 (6c broker, 6d admin, 7 webhook-ingress)" |
| `self-managed-deploy.md` § "Phase 3b: Stage-by-stage steps" | Update the table to note which scripts are now also chained by deploy-all.sh (rather than "stage-by-stage only") |
| `CLAUDE.md` § "Deployment Playbook" | No changes needed — it already says "follow deploy-quickstart.md" and notes the placeholder-artifact rule. The new steps call the existing per-phase scripts (no logic duplication). |

### Cross-references to add/update

- `deploy-all.sh` should have a comment at the top referencing the canonical
  phase list: "See deploy-quickstart.md for the phase-by-phase guide."
- `deploy-webhook-ingress.sh` header currently says "NOT deployed by
  deploy-all.sh" — update to say "Deployed by deploy-all.sh Step 10 (or run
  standalone)."

---

## 7. Implementation Constraints

1. **Each phase MUST call the existing per-phase script** — no logic duplicated
   inline. The scripts handle their own environment resolution, error handling,
   and idempotency. `deploy-all.sh` passes `--env $ENVIRONMENT` and
   `--region $AWS_REGION`.

2. **No AWS account IDs in the script or docs** (per #3314). The script
   continues to derive the account via `load-deploy-config.sh` / `aws sts
   get-caller-identity`. The per-phase scripts do the same.

3. **Non-interactive shell rules apply** (per CLAUDE.md). All called scripts are
   already non-interactive by default. `register-github-app.sh` is NOT called
   because it has an interactive browser step.

4. **Step numbering** must be consistent throughout — no mixed `/6` and `/8`.
   After the update: `/11` everywhere (or `/N` where N is dynamically
   determined by which steps are enabled — simpler to just use `/11`).

---

## 8. Implementation Sketch (for the developer agent)

```bash
# After Step 6 (frontend), before agent-factory:

# ─── Step 7/11: Broker Lambda code ───
if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SKIP_BROKER" = false ]; then
  step "Step 7/11: Deploy broker Lambda code"
  bash "$ROOT_DIR/modules/gateway/scripts/deploy-broker.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "Broker Lambda deployed"
fi

# ─── Step 8/11: Bootstrap first admin ───
if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SKIP_ADMIN_BOOTSTRAP" = false ]; then
  step "Step 8/11: Bootstrap first admin"
  bash "$ROOT_DIR/modules/gateway/scripts/bootstrap-admin.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "First admin bootstrapped"
fi

# ... existing agent-factory (now Step 9/11) ...

# ─── Step 10/11: Webhook-ingress stack ───
if [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SKIP_WEBHOOK_INGRESS" = false ]; then
  step "Step 10/11: Deploy webhook-ingress stack"
  bash "$ROOT_DIR/modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh" \
    --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "Webhook-ingress deployed"
fi
```

The summary section at the end should add:
```bash
echo ""
echo "━━━ Next steps (manual) ━━━"
echo "To complete the agent path:"
echo "  1. Log in as platform_admin → Settings → Connections → 'Set up GitHub App'"
echo "     (or CLI: modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env $ENVIRONMENT)"
echo "  2. Install the App on target repo(s)"
echo "  3. Comment '@agent-developer <task>' on an issue to trigger an agent"
```

---

## 9. Risk Assessment

| Risk | Likelihood | Mitigation |
|------|:----------:|------------|
| broker deploy fails mid-script (Lambda doesn't exist yet) | Low | `deploy-broker.sh` checks the Lambda exists; the `set -e` in deploy-all.sh halts on failure. Operator re-runs. Broker only exists after gateway-infra (step 3), which runs first. |
| bootstrap-admin.sh fails (gateway pod not ready) | Medium | The gateway rollout check in step 4 already waits up to 300s. If it times out with a warning, bootstrap-admin.sh will fail. Mitigation: add a `kubectl rollout status` gate before calling it (or accept the failure and re-run). |
| webhook-ingress deploy fails (agent-factory infra not complete) | Low | Step 9 (agent-factory) runs terraform first; step 10 follows. Hard dep satisfied by ordering. |
| Operator expects "deploy all" to include GitHub App wiring | Medium | Clear messaging in the summary section + `--help` output explains the manual step. |

---

## 10. Verdict

✅ **Design ready for implementation.** No blocking issues. The implementation
is straightforward (3 new script calls + flag additions + step renumbering) and
carries low risk due to the idempotency of all called scripts.

The developer agent should:
1. Add the 3 new flags (`--skip-broker`, `--skip-admin-bootstrap`, `--skip-webhook-ingress`)
2. Renumber all steps to `/11`
3. Insert the 3 new phases at the correct positions (after frontend, after agent-factory)
4. Add the "Next steps" summary for GitHub App wiring
5. Update the `--help` text
6. Update cross-references in `self-managed-deploy.md` and `deploy-quickstart.md`
7. Run `bash -n` + shellcheck on the modified script
