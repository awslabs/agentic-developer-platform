# Platform Upgrades

How to update an already-deployed ADP platform to newer code — the operator's
guide to `--update` mode, the guarantees it makes, and what to do when
something refuses to apply.

**Audience:** operators of self-managed, customer-linked, or internal
environment accounts. The ADP platform account itself is NOT updated this way —
it stays on the CI pipelines (`gateway-deploy.yml`, `*-infra-apply.yml`, etc.)
triggered by merges to `main`.

**Design reference:** [`deploy-all-update-mode-design.md`](./deploy-all-update-mode-design.md)
(issue #3414/#3528). First live validation: issue #3565 (2026-07-11), which
converged a 5-day-old deployment end-to-end.

---

## 1. TL;DR — the standard upgrade

```bash
# From a clean checkout of the code you want to deploy (main or a pinned tag):
git pull origin main            # or: git checkout <tag>

# Credentials must resolve to the TARGET account, not your default profile:
aws sts get-caller-identity --query Account --output text   # verify!

# Run the upgrade:
./platform/scripts/deploy-all.sh --update
```

That's the whole happy path. The script plans everything first, applies only
non-destructive changes, builds SHA-tagged images, rolls out the backend with
mandatory health verification, and runs database migrations. A no-change
convergence takes ~3–5 minutes; a real update takes ~15–30 minutes (dominated
by CodeBuild image builds).

**What stops the run:** a Terraform plan containing resource **destroys**. That
is deliberate — see §5.

---

## 2. What "update mode" is (and is not)

`deploy-all.sh` has four modes; `--update` is the only one for upgrading:

| Mode | Command | Use when |
|------|---------|----------|
| Fresh deploy | `./platform/scripts/deploy-all.sh` | Standing up a new account from scratch |
| **Update** | `./platform/scripts/deploy-all.sh --update` | **Converging an existing deployment to newer code** |
| Destroy | `--destroy` (legacy — prefer `undeploy.sh`) | Tearing everything down |
| CI validate | `--ci` | Pipeline output validation, no applies |

`--update` is **mutually exclusive** with `--destroy` and `--ci` — the script
refuses the combination.

Update mode is an **explicit flag, not auto-detected**. A partially-failed
fresh deploy leaves a state bucket but not a working platform; auto-detecting
"update" there would skip recovery steps. If you run `--update` against an
account that was never deployed, the preconditions (§3) fail fast with a clear
message — nothing is touched.

### Differences from a fresh deploy

| Aspect | Fresh deploy | `--update` |
|--------|--------------|------------|
| Bootstrap (state bucket / lock table) | Creates if missing | **Skipped** — must already exist |
| Bedrock model agreements | Runs | Skipped (slow, already done) |
| Terraform applies | `-auto-approve` | **Plan-first with destroy gate** (§5) |
| Image tag | `:latest` | **Source SHA** (`git rev-parse --short=12 HEAD`) |
| Backend rollout | Best-effort | **Mandatory** — script fails if rollout fails |
| Database migrations | Not run | **Runs alembic** pre- and post-rollout (§6) |
| Admin bootstrap | Seeds first admin | Skipped — admin already exists |
| GitHub App prompt | Shown at end | Not shown — already configured |
| Preflight | Full (~27 checks) | Partial (AWS auth + cluster reachability) |

---

## 3. Preconditions

Update mode fail-fasts before touching anything if the target does not look
like a live deployment:

1. **State bucket exists** (`adp-terraform-state-<account-id>`)
2. **EKS cluster is `ACTIVE`** (`adp-<env>-eks-cluster`)
3. **`adp-gateway` namespace exists** (evidence of a prior gateway deploy)

Also verify yourself, before running:

- **You are in the right account.** `aws sts get-caller-identity` must resolve
  to the account you intend to upgrade. If you operate multiple linked
  accounts, this is the single most important check — see §9.
- **Clean checkout.** The image tag comes from `git rev-parse HEAD`; a dirty or
  wrong-branch checkout deploys something other than what you think. Deploy
  from `main` or a pinned release tag.
- **Do not commit tfvars rewrites.** The script sed-substitutes account IDs
  into `environments/**/*.tfvars` in your working tree. These are deploy-time
  artifacts — never commit them (they would point everyone's Terraform
  backends at your account).

---

## 4. Command reference

### Modes and update-specific flags

```
./platform/scripts/deploy-all.sh --update [flags]

  --update               Converge an existing deployment to newer code
  --confirm-destructive  Authorize terraform applies that include resource
                         destroys (see §5 before using — this is global for
                         the whole run, not per-module)
```

### Scope flags (compose with --update)

```
  --gateway-only         Platform + gateway only
  --agent-factory-only   Platform + agent-factory only
  --agent-context-only   Platform + agent-context only
```

⚠️ **agent-context caution:** do not include agent-context in an update unless
you know its Terraform state is clean — historical drift there (Neptune) plans
multi-resource destroys. The destroy gate will refuse, which is correct;
investigate rather than override.

### Skip flags (compose with --update)

```
  --skip-frontend        Skip frontend build + S3 sync + CloudFront invalidation
  --skip-broker          Skip broker Lambda code deploy
  --skip-webhook-ingress Skip the webhook-ingress stack
  --skip-agent-context   Skip agent-context even if AGENT_CONTEXT_ENABLED=true
```

(`--skip-admin-bootstrap` exists but is redundant with `--update`, which skips
admin bootstrap anyway.)

### Common invocations

```bash
# Full upgrade (everything that is deployed):
./platform/scripts/deploy-all.sh --update

# Gateway-only upgrade, no frontend rebuild (fastest meaningful update):
./platform/scripts/deploy-all.sh --update --gateway-only --skip-frontend

# Convergence check — "is this account at parity with my checkout?"
# A fully-converged account produces no-op plans, an alembic no-op, and a
# digest-match fast-path restart. ~3-5 min:
./platform/scripts/deploy-all.sh --update --gateway-only --skip-frontend

# After reviewing a refused plan and confirming every destroy is benign:
./platform/scripts/deploy-all.sh --update --confirm-destructive
```

---

## 5. The Terraform destroy gate

In update mode, every Terraform apply is replaced by a **plan-first gate**
(`terraform_update_apply`):

1. `terraform plan -detailed-exitcode -no-color` runs and the output is saved.
2. **No changes** → apply is skipped (fast no-op).
3. **Changes, zero destroys** → applied automatically. Update mode is
   *plan-first but non-interactive* — you are not prompted for safe changes.
4. **Any destroy** → the script **refuses and exits**, printing the destroy
   lines and hints about known landmines.

### When the gate refuses

Read the printed plan excerpt and classify every destroy:

**Usually benign (churn, safe to confirm):**
- `aws_api_gateway_deployment` deposed-object cleanup
- Security-group **rule** count changes
- IAM policy destroy+create where a `moved` block is missing (e.g. the
  gateway `kms:Decrypt` policy — issue #2909)
- CloudFront VPC-origin recreation *(should no longer appear as of #3665 —
  if it does, that fix has regressed; file it)*

**Never confirm without investigation — stop and escalate:**
- Anything touching **RDS, DynamoDB tables, Cognito, S3 buckets, EKS,
  Neptune, SQS queues** — these hold state or identity; a destroy is data
  loss or an outage.
- Destroy counts that look like whole-module teardown (e.g. Neptune's
  9-destroy drift pattern).

If — and only if — **every** destroy across **all** refused modules is on the
benign list, re-run with `--confirm-destructive`. The flag is **global for the
run**: it authorizes destroys in every module the run applies, so evaluate the
complete set, not just the first refusal.

### Known gate limitations

- The gate covers the six Terraform applies inside `deploy-all.sh`. The
  **webhook-ingress stack** is applied by a delegated script with its own
  `-auto-approve` and is NOT gated (issue #3543, open). Until that closes,
  cautious operators should run `--skip-webhook-ingress` and converge
  webhook-ingress manually:

  ```bash
  cd modules/agent-factory/webhook-ingress/infra
  terraform plan -var-file=... -no-color | grep -c 'will be destroyed'
  # 0 destroys → terraform apply; any destroys → stop, investigate
  ```

- History note: the gate's destroy detection was broken (ANSI color codes
  defeated the grep) from its introduction until #3664 (2026-07-11). Runs
  before that date silently bypassed the gate. Regression-tested since
  (`platform/scripts/tests/test-destroy-gate.sh`).

---

## 6. What happens to images, rollouts, and migrations

**Images.** Update mode tags images with the source SHA
(`IMAGE_TAG=$(git rev-parse --short=12 HEAD)`) for `adp-gateway`,
`adp-agent-gateway`, and `adp-agent-runtime`, and forwards that tag to
CodeBuild. This guarantees Kubernetes sees a new image reference and actually
rolls out — the classic `:latest`-push-no-rollout silent failure cannot happen.
If the digest already matches (no code change), the script takes a
`rollout restart` fast path instead of rebuilding.

**Rollouts.** `kubectl rollout status` is mandatory — no `|| true`. A failed
rollout fails the run. After rollout, a health check must pass
(`/health` returns healthy) before the script continues.

**Migrations.** Alembic runs twice:
- **Pre-rollout**: brings the DB to the head known to the *currently running*
  image (usually a no-op).
- **Post-rollout**: runs `alembic upgrade head` inside the **newest** gateway
  pod (selected by creation timestamp, `-l app=bedrockgateway`) — this is the
  step that applies migrations *introduced by the new code*, which the old
  image did not contain.

> Note: the design doc's §3 originally specified migrations before rollout
> only; the implementation corrected this to pre+post because a new revision's
> migration files only exist inside the new image. The post-rollout step is
> the load-bearing one.

**Frontend.** Rebuilt (`npm ci && npm run build` with the correct
`VITE_API_URL`), synced to S3, CloudFront invalidated — unless
`--skip-frontend`.

**Broker Lambda.** Code package rebuilt and uploaded (Step 7) unless
`--skip-broker`.

---

## 7. Verifying an upgrade

After the script exits 0:

```bash
# 1. The new image is actually running (tag = your checkout's SHA):
kubectl get deploy bedrockgateway -n adp-gateway \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
git rev-parse --short=12 HEAD    # must match the tag above

# 2. Pods healthy:
kubectl get pods -n adp-gateway

# 3. Migrations at head:
kubectl exec -n adp-gateway deploy/bedrockgateway -- alembic current

# 4. API healthy THROUGH the CDN (asserts the body, not just a 200 —
#    S3-served SPA fallback also returns 200 on errors):
CF_DOMAIN=$(aws ssm get-parameter --name "/adp/dev/gateway/cloudfront-domain" \
  --query "Parameter.Value" --output text)
curl -s "https://${CF_DOMAIN}/api/health"    # expect {"status":"healthy"}

# 5. Dashboard loads:
curl -s -o /dev/null -w "%{http_code}\n" "https://${CF_DOMAIN}/"
```

**Idempotency check (optional but recommended after a big update):** re-run
the same `--update` command. Expect no-op plans, alembic no-op, digest-match
restart, healthy — in ~3–5 minutes. Churn on the second run indicates drift
worth investigating.

**Agent-path smoke (if agents run against this account):** @-mention an agent
on a trivial test issue and confirm webhook → worker job → PR. Watch the
agent-submit DLQ for stuck messages during the smoke.

---

## 8. Failure handling and rollback

| Failure | What to do |
|---------|-----------|
| Precondition fail | You're in the wrong account, or the platform was never deployed there. Fix credentials / use fresh-deploy mode. |
| Destroy-gate refusal | Classify per §5. Benign → `--confirm-destructive`. Stateful → stop, investigate the drift, involve whoever owns the module. |
| CodeBuild failure | `aws codebuild batch-get-builds --ids <id> --query 'builds[0].logs.deepLink'`. Transient → re-run the script from the top (idempotent). |
| Rollout failure | Script halts (by design). `kubectl logs -n adp-gateway -l app=bedrockgateway --previous --tail=50`. Usually a config/schema issue — check post-rollout migration output. |
| Migration failure | The DB may be mid-upgrade. Do NOT re-run blindly — inspect `alembic current` vs `alembic heads`, fix, then re-run. |
| STS/credential expiry mid-run | Re-assume and re-run from the top. Completed phases no-op through. |

**Rollback = deploy the previous code.** Because images are SHA-tagged and
migrations are additive by convention:

```bash
git checkout <previous-sha-or-tag>
./platform/scripts/deploy-all.sh --update
```

This re-points the deployment at the old image via a normal rollout. Terraform
state is only touched if a gated apply ran — plans that were refused changed
nothing. Migrations are the caveat: alembic downgrades are not run
automatically; if the bad upgrade included a migration, assess whether the old
code tolerates the new schema (additive migrations usually do) before rolling
back.

---

## 9. Upgrading via a hosted agent (ADP-managed track)

Operators of the ADP-managed track drive upgrades through a **deploy-instance
issue** (see the #3565 runbook pattern, EPIC #2571) instead of a terminal:

1. File a runbook issue specifying the target account, the exact `--update`
   command, the destroy-gate policy, and validation steps.
2. Dispatch with an `@agent-operations` mention **and the `/aws-label`
   directive in the same comment** to pin which linked AWS account the agent's
   credentials resolve to:

   ```
   @agent-operations run the update per this runbook.

   /aws-label adp-integration-test
   ```

   Without `/aws-label`, the agent gets the triggering user's
   most-recently-verified linked account — which may not be the one you mean.
   The label must match the vault label shown in the dashboard (Settings →
   Connections). An unknown label fails the run loudly rather than falling
   back (by design).
3. The runbook must include the **hard target-account guard**: assert
   `aws sts get-caller-identity` equals the expected account before every
   phase, and stop on mismatch.
4. Run artifacts are **status comments on the issue** — the agent must never
   open a PR from its deploy working tree (the tfvars account substitutions
   must not be committed).

---

## 10. What update mode does NOT cover

- **The ADP platform account** — CI pipelines own it; never run `--update`
  against it.
- **Webhook-ingress destroy-gating** — see §5 (issue #3543).
- **A `--plan-only` preview** — there is no dry-run flag yet; the plan-gate
  output during a run is the preview. (Deferred follow-on from #3414.)
- **Alembic downgrades** — rollback relies on additive migrations (§8).
- **GitHub App changes** — App registration/installation is UI-driven and
  independent of code upgrades.
- **Per-account Bedrock model agreements** — assumed already enabled; only
  fresh deploys run the enablement script.

---

## Related docs

- [`self-managed-deploy.md`](./self-managed-deploy.md) — fresh-deploy sequence (this doc's §1 command appears there as "Updating an existing deployment")
- [`deploy-all-update-mode-design.md`](./deploy-all-update-mode-design.md) — full design rationale (#3414)
- [`deployment-manifest.md`](./deployment-manifest.md) — per-resource validation commands
- [`adp-managed-deploy.md`](./adp-managed-deploy.md) — the hosted-agent track
- Issues: #3528 (implementation), #3565 (first live run), #3664/#3665/#3666 (fixes from that run), #3543 (open webhook-ingress gate gap)
