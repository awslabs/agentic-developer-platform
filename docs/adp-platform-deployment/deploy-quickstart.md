# ADP Deploy — Quick Start (Simplified)

A no-nonsense path from a fresh clone to a running platform. This guide is the
*simple* version — it tells you the three commands that matter, the choices you
actually have to make, and the gotchas that will bite you (especially on macOS).

For the exhaustive resource-by-resource reference, see
[`self-managed-deploy.md`](./self-managed-deploy.md) and
[`deployment-manifest.md`](./deployment-manifest.md). For a real run's log of what
broke and how it was fixed, see the `deployment_learning_<accountID>.md` at the
repo root.

> **Status of this guide:** Phases 1–6c (Bootstrap → Preflight → Platform infra →
> Gateway infra → Gateway backend → Frontend → ALB second-pass → Broker) plus the
> webhook agent path are **verified end-to-end against a real run** (account
> `919157478356`, macOS, 2026-06-06/07). Each step is backed by a re-runnable
> script. Bugs
> found during that run are **fixed on `main`** across PRs #1210 (sed/preflight/CI),
> #1218 (gateway count arg + Lambda-layer auto-build + gateway CI), and #1221
> (VPC-endpoint SG for IRSA) — if you're on latest `main`, the gotchas called out
> below no longer bite; they're kept to explain behavior and for older checkouts.
> Phases 6–9 are transcribed from the full guide and not yet re-verified here
> (marked ⚠️ *unverified*). Agent Factory phases (CLAUDE.md 4–5) were not exercised
> in the gateway-only run.

---

## The shape of a deploy

A deploy is a sequence of **stage-by-stage scripts** (each idempotent, each one
phase below). GitHub is NOT set up upfront — for the webhook agent path, all
GitHub wiring happens at the **end** (Phase 8), after the infrastructure it
points at exists. The infra phases:

```bash
export AWS_PROFILE=<profile> AWS_REGION=us-east-1   # the account everything keys off
./platform/scripts/bootstrap.sh                     # Phase 1
./platform/scripts/preflight-check.sh               # Phase 2
# Phase 3–6: terraform applies + image/frontend builds (see each phase below)
./platform/scripts/wire-gateway-alb.sh --apply      # Phase 6b (gateway second pass)
./modules/gateway/scripts/deploy-broker.sh --env dev  # Phase 6c (login)
# Agent path (optional):
./modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh --env dev   # Phase 7
./modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env dev  # Phase 8
```

`deploy-all.sh` chains Phases 1–6 (incl. the ALB pass) but **not** the webhook
path / broker / agent-runtime — use the stage-by-stage scripts for those. The
rest of this doc is **what to know before each phase** and **what to do when it
breaks**.

---

## Before you start: 4 things that will save you an hour

1. **Pick the right AWS profile FIRST — everything keys off it.**
   There is no `account_id` hardcoded anywhere. Every script resolves the target
   account in this order: **env var → `config/deployment.yml` → whatever
   `aws sts get-caller-identity` returns**. If `config/deployment.yml` doesn't
   exist yet, the *active AWS profile decides the account*. So:
   ```bash
   export AWS_PROFILE=<your-profile>
   export AWS_REGION=us-east-1
   aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
   ```
   Confirm that account is the one you intend to deploy into. Get this wrong and
   you bootstrap state into the wrong account.

2. **Ignore any committed `.adp-deploy-state.json`.**
   This file is git-tracked in the repo, so a fresh clone may show phases marked
   `"complete"` and an `account_id` like `ACCOUNT_ID` (a literal placeholder) or
   some *other* account. **It is not a record of your deployment.** Don't trust
   its statuses — verify against real AWS state instead. Treat a fresh clone as a
   fresh deployment regardless of what this file says.

3. **macOS sed — fixed on `main`, but check your checkout.** `bootstrap.sh` used
   to call GNU-style `sed -i -e "..."`, which BSD/macOS sed rejects with
   `sed: -e: No such file or directory` and *silently skipped* the tfvars rewrite.
   **PR #1210 fixed this** (the script now detects GNU vs BSD sed). If you're on
   latest `main` you're fine. If you're on an older checkout and see that error,
   the substitution didn't run — redo it by hand (snippet in Phase 1). Running
   from Linux/WSL2 avoids it entirely either way.

4. **Temporary STS creds expire.** If your profile uses `ASIA…` keys + a session
   token (e.g. assumed-role / Isengard / SSO), they time out mid-deploy. A 45-min
   deploy can outlive them. Refresh before long phases, and re-`export` the
   profile.

**Tooling needed:** aws-cli v2, terraform ≥ 1.14, kubectl, node ≥ 22, `gh`
(for GitHub steps). **Docker is optional** — `deploy-all.sh` builds images with
CodeBuild by default; you only need Docker if you pass `--local`.

---

## Choose your scope

| You want | Command | GitHub Apps? | Rough idle cost |
|----------|---------|--------------|-----------------|
| Everything | `deploy-all.sh` | Yes | ~$170/mo |
| Bedrock gateway only | `deploy-all.sh --gateway-only` | **No** ← simplest | ~$170/mo |
| Agents only | `deploy-all.sh --agent-factory-only` | Yes | lower (no RDS) |
| Code intelligence | `deploy-all.sh --agent-context-only` | No | ~$800/mo ⚠️ pricey |

If you just want to see the platform work with the least setup,
**`--gateway-only`** is simplest — no GitHub involvement at all.

> **No upfront GitHub setup.** Older docs had a "Phase 0" that ran `setup-org.sh`
> + `create-github-apps.sh` first. That was the legacy **ARC** track (3 org-owned
> apps, repo push). It's removed here: the webhook agent path wires GitHub at the
> **end** (Phase 8, `register-github-app.sh`), after the infra it points at
> exists. The only thing you need before Phase 1 is the right **AWS profile**
> (above) — `config/deployment.yml` is optional (account resolves from the
> profile if absent).

---

## Phase 1 — Bootstrap ✅ verified

Creates the Terraform state backend. **This is the first thing `deploy-all.sh`
does**, but you can run it standalone:

```bash
export AWS_PROFILE=<your-profile>
export AWS_REGION=us-east-1
export ENVIRONMENT=dev
./platform/scripts/bootstrap.sh
```

Creates (idempotent — re-runnable):
- S3 bucket `adp-terraform-state-<account-id>` (versioned, AES256, public access blocked)
- DynamoDB table `adp-terraform-locks` (PAY_PER_REQUEST)

### macOS sed (fixed on `main`, PR #1210)

The script's **last step** rewrites `environments/**/*.tfvars` to bake in your
account id. It used GNU `sed -i -e`, which failed on macOS
(`sed: -e: No such file or directory`) and silently left the `ACCOUNT_ID`
placeholder in the tfvars — breaking the next `terraform init`. **PR #1210 made
the script sed-portable**, so on latest `main` this just works.

**Only if you're on an older checkout and hit it:** redo the substitution by hand
(this is exactly what the fixed script now does internally):
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
find environments/ -name "*.tfvars" -exec sed -i '' \
  -e "s/ACCOUNT_ID/${ACCOUNT_ID}/g" \
  -e "s/adp-terraform-state-[0-9]\{12\}/adp-terraform-state-${ACCOUNT_ID}/g" {} \;
```

> **Do NOT commit the tfvars rewrite.** It's a per-operator local substitution;
> committing it would lock the repo to one account. (This is also why CI keeps the
> `ACCOUNT_ID` placeholder and resolves the real bucket at runtime — see Phase 3.)

**Verify:**
```bash
aws s3api head-bucket --bucket "adp-terraform-state-$(aws sts get-caller-identity --query Account --output text)"
aws dynamodb describe-table --table-name adp-terraform-locks --query 'Table.TableStatus' --output text   # ACTIVE
grep "$(aws sts get-caller-identity --query Account --output text)" environments/dev/backend.tfvars        # matches
```

---

## Phase 2 — Preflight ✅ verified

```bash
export AWS_PROFILE=<your-profile>   # required — preflight resolves account from it
export AWS_REGION=us-east-1
./platform/scripts/preflight-check.sh
```

Read-only. ~26 checks across CLI tools, AWS creds, IAM perms, and whether the
state bucket + lock table from Phase 1 are reachable. **FAILs block; WARNs don't.**
A WARN about a missing service permission (iam/codebuild/bedrock/secrets/cognito)
is *predictive* — the phase that uses that service later will fail, so fix it
before you get there.

### CodeBuild/Bedrock false-alarm warnings (fixed on `main`, PR #1210)

The CodeBuild and Bedrock permission checks used **invalid CLI syntax**
(`--max-results 1`, which neither subcommand accepts), so they errored on *usage*
and got misreported as permission warnings even when the permissions were present.
**PR #1210 removed the bad flag**, so on latest `main` these checks report
accurately. On an older checkout, confirm manually before chasing a phantom
permissions problem:
```bash
aws codebuild list-projects                                   # {"projects": []} = fine
aws bedrock list-foundation-models --query 'modelSummaries[0].modelId'   # a model id = fine
```

After the fix, a clean run looks like **24 passed / 2 warnings / 0 failed** — the
2 remaining warnings are "EKS cluster not found" and "ECR adp-gateway not found",
both expected and both created in Phase 3.

---

## Phase 3 — Platform infra ✅ verified (with caveats)

Standalone (what `deploy-all.sh` automates):
```bash
cd platform/infra
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
terraform init -backend-config=../../environments/dev/backend.tfvars -input=false -reconfigure
MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
export TF_VAR_eks_public_access_cidrs="[\"${MY_IP}/32\"]"   # locks EKS API to you
terraform apply -var-file=../../environments/dev/platform.tfvars -auto-approve
```
Creates ~94 resources: VPC, EKS (Auto Mode, v1.35), ECR repos, IAM, CodeBuild
projects, security-scans bucket. ~10–15 min.

### ⚠️ Gotchas hit on a real run

- **Never pipe `terraform apply` to `tail` and trust the exit code** — you'll get
  `tail`'s exit code (0) even when terraform failed. Use
  `terraform apply ... > log 2>&1; echo $?` or check `${PIPESTATUS[0]}`.
- **EKS namespace can fail on first apply** with
  `namespaces is forbidden: ... cannot create resource "namespaces"` — the
  cluster-admin access entry hasn't propagated yet. **Just re-run apply** (idempotent).
- **Non-ASCII in a security group description fails AWS validation** —
  `CreateSecurityGroup` rejects descriptions with characters beyond ASCII
  (`Character sets beyond ASCII are not supported`). One SG description had an
  em-dash; **fixed on `main` (PR #1210)**. Flagged here because it's an easy trap
  to reintroduce — keep IaC strings ASCII-only.
- **0 nodes after apply is normal** — Auto Mode provisions nodes on demand.

Verify:
```bash
aws eks describe-cluster --name adp-dev-eks-cluster --query 'cluster.status'   # ACTIVE
aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1
kubectl get ns       # bedrockgw should be Active
```

> **CI note:** the `*-infra-plan` PR checks used to fail at `terraform init`
> (`S3 bucket "adp-terraform-state-ACCOUNT_ID" does not exist`) because they
> passed the committed placeholder `backend.tfvars` without substituting the
> account. **Fixed for platform (#1210), gateway (#1218), and webhook-ingress
> (#1224)** — those workflows now resolve the bucket from the runner's own
> identity at runtime. The remaining siblings (`agent-context` / `agent-factory`
> / `cyber` -infra-plan) still have this bug; fix pending.

## Phase 4 — Gateway infra (RDS/Redis/Cognito/CloudFront) ✅ verified

```bash
cd modules/gateway/infra
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
terraform init -backend-config=../../../environments/dev/modules/gateway-backend.tfvars -reconfigure
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars -auto-approve
```
Creates ~155 resources: RDS PostgreSQL, ElastiCache Redis, Cognito, CloudFront,
API Gateway, KMS, and the budget/authorizer/auth-broker Lambdas + chat-logging
pipeline. RDS provisioning dominates (~10 min).

### ⚠️ Two plan-time prerequisites (both now auto-handled on latest `main`)

- **Lambda layers must exist in S3 first.** `budget-lambda` (psycopg2) and
  `lambda-authorizer` (pyjwt) read `s3://<state-bucket>/lambda-layers/*.zip` via
  `aws_s3_object` data sources that resolve at **plan** time → apply fails with
  `couldn't find resource` if absent. **PR #1218 fixed this**: the gateway module
  now builds the layers itself (null_resource → CodeBuild) on every deploy path.
  Older checkout / manual: `./platform/scripts/build-lambda-layers.sh psycopg2 pyjwt`.

- **GitHub auth broker needs a Secrets Manager secret.** With
  `enable_github_auth_broker = true`, a `data "aws_secretsmanager_secret"` reads
  `adp/<env>/cognito/github-oauth-credentials` at plan time → plan fails if it
  doesn't exist. Provision it (real GitHub OAuth App creds, or a placeholder to
  stand up infra):
  ```bash
  aws secretsmanager create-secret --name adp/dev/cognito/github-oauth-credentials \
    --secret-string '{"client_id":"...","client_secret":"..."}' --region us-east-1
  ```
  Or set `enable_github_auth_broker = false` to skip GitHub login. `deploy-all.sh`
  now fails fast with this exact guidance instead of a cryptic data-source error.

- **`Invalid count argument`** when `enable_api_gateway` + `enable_github_auth_broker`
  are both true — **fixed on `main` (PR #1218)**. (Was: a `count` keyed off the
  broker Lambda's apply-time-computed ARN.)

Verify:
```bash
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].DBInstanceStatus' --output text   # available
aws cognito-idp list-user-pools --max-results 10 --query 'UserPools[?starts_with(Name,`bedrockgw`)].Name' --output text
aws cloudfront list-distributions --query 'DistributionList.Items[?starts_with(Comment,`bedrockgw`)].Status' --output text          # Deployed
```

## Phase 5 — Gateway backend on EKS ✅ verified (the hard one)

Build the image (CodeBuild, no local Docker), deploy to EKS, render configmap/SA
from terraform outputs, rollout. `deploy-all.sh` Step 4 automates this.

### ⚠️ The big gotcha: IRSA pods can't reach STS → everything hangs

On a fresh cluster the gateway pods **CrashLoopBackOff**, stuck in startup
(`/health` connection-refused). Root cause is **not** the app — it's that
**EKS Auto Mode pods use the managed cluster security group, but the VPC
interface-endpoint SG (`adp-dev-sg-vpce`) only allowed the custom EKS SG.** So
pods can't reach the **STS** interface endpoint → IRSA `AssumeRoleWithWebIdentity`
hangs → RDS IAM-auth token generation hangs → the FastAPI lifespan `create_all`
hangs → liveness kills the pod. Same SG-mismatch the repo already works around
for RDS/Redis; the VPC-endpoints SG was missed.

**Symptoms / how to confirm:**
```bash
# From an IRSA pod (use a stable sidecar with serviceAccountName: gateway-service,
# NOT the crashlooping pod — exec gets SIGKILLed mid-restart):
aws sts get-caller-identity        # HANGS (~15s timeout) = STS unreachable
# DNS resolves to the interface-endpoint private IPs, but TCP 443 to it times out:
timeout 8 bash -c 'echo > /dev/tcp/sts.us-east-1.amazonaws.com/443' || echo BLOCKED
```

**Fix (merged to `main`, PR #1221):** add an ingress rule allowing 443 on the
VPC-endpoints SG from the EKS managed cluster SG. Immediate unblock:
```bash
VPCE_SG=$(aws ec2 describe-vpc-endpoints --filters Name=service-name,Values=com.amazonaws.us-east-1.sts \
  --query 'VpcEndpoints[0].Groups[0].GroupId' --output text)
CLUSTER_SG=$(aws eks describe-cluster --name adp-dev-eks-cluster \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "$VPCE_SG" --protocol tcp --port 443 --source-group "$CLUSTER_SG"
kubectl rollout restart deployment/bedrockgateway -n adp-gateway
```

### Second gotcha: RDS IAM auth needs the `rds_iam` GRANT

The gateway authenticates to RDS with an IAM token as user `bgadmin` (the RDS
master user). That requires `GRANT rds_iam TO bgadmin` inside Postgres — done by
the **`rds-bootstrap` k8s job**. If that job hasn't succeeded (it also needs STS
reachable — see above), IAM auth fails. Verify / fix manually:
```bash
# As master (creds in the rds!… secret), check + grant:
psql "host=<rds> user=bgadmin dbname=bedrockgateway sslmode=require" \
  -tAc "SELECT pg_has_role('bgadmin','rds_iam','MEMBER');"   # 'f' = not granted
psql "...as master..." -c "GRANT rds_iam TO bgadmin;"
# After the grant, bgadmin's master *password* stops working (PAM auth) — expected.
```

### Other Phase 5 notes
- Image builds via CodeBuild `adp-dev-gateway-build` (~90s). No local Docker.
- Old replicaset pods may show `InvalidImageName` (the manifest ships a
  `REPLACE_WITH_GATEWAY_IMAGE` placeholder); `kubectl set image` fixes the live
  one — cosmetic once the new RS is healthy.
- Verify: `kubectl exec deploy/bedrockgateway -n adp-gateway -- curl -s localhost:8080/health` → `{"status":"healthy"}`.

## Phase 6 — Frontend (React → S3 → CloudFront) ✅ verified

**⚠️ Build with the FULL `VITE_*` env — not just `VITE_API_URL`.** Vite vars are
baked into the bundle at build time. If you build with only `VITE_API_URL`, the
deployed app shows *"GitHub sign-in is not configured (VITE_GITHUB_AUTH_BROKER_URL
not set)"* and Cognito login is unconfigured too. All the values come from SSM
params that **Phase 4 already created** (so Phase 6 can build correctly the first
time — no rebuild needed; clicking GitHub login works once Phases 6b/6c make the
broker live). This mirrors what `gateway-deploy.yml` passes.

```bash
cd modules/gateway/frontend
npm ci
get_ssm() { aws ssm get-parameter --name "$1" --query Parameter.Value --output text 2>/dev/null; }

VITE_API_URL="/api" \
VITE_COGNITO_REGION="us-east-1" \
VITE_COGNITO_USER_POOL_ID="$(get_ssm /adp/dev/gateway/cognito-user-pool-id)" \
VITE_COGNITO_CLIENT_ID="$(get_ssm /adp/dev/gateway/cognito-client-id)" \
VITE_COGNITO_DOMAIN="$(get_ssm /adp/dev/gateway/cognito-domain)" \
VITE_GITHUB_AUTH_BROKER_URL="$(get_ssm /adp/dev/gateway/github-auth-broker-url)" \
VITE_AGENT_WS_URL="$(get_ssm /adp/dev/gateway/agent-ws-url)" \
  npm run build       # VITE_API_URL is "/api" — NOT /api/gateway

BUCKET=$(get_ssm /adp/dev/gateway/frontend-bucket)
DIST=$(get_ssm /adp/dev/gateway/cloudfront-id)
aws s3 sync dist/ "s3://${BUCKET}/" --delete
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*"
```

Notes: `VITE_AGENT_WS_URL` may be empty (the agent-gateway WebSocket isn't part
of the webhook path) — the app tolerates it. Node 24 builds fine (repo says ≥ 22).
If you ever change the broker/Cognito values, you must **rebuild + re-sync** —
they're compiled in, not read at runtime.

Verify:
```bash
CF=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text)
curl -s -o /dev/null -w "%{http_code}\n" "https://${CF}/"        # 200
curl -s "https://${CF}/" | grep -o "<title>[^<]*</title>"       # <title>Agentic Developer Platform</title>
```

## Phase 6b — Gateway second pass (wire ALB) ✅ verified — REQUIRED

The gateway API Gateway ships a **MOCK** OpenAPI body until the EKS Ingress ALB
is wired — so on first apply there's **no backend `/{proxy+}` route and no
`/auth/github` broker route** (only `/` and `/status`). This second pass switches
it to the real body. **Skipping this leaves the API/login broken.**

```bash
ENVIRONMENT=dev bash platform/scripts/wire-gateway-alb.sh --apply
```
This discovers the ALB, re-applies gateway-infra with the ALB vars
(`internal_alb_arn/dns`, `alb_security_group_ids`, `enable_vpc_origin=true`), and
forces an API GW stage redeploy so the routes go live. Idempotent. (Note: the
CloudFront VPC origin it creates can take ~10 min.)

Verify:
```bash
aws apigateway get-rest-apis --query "items[?name=='bedrockgw-dev-api'].id" --output text  # API id
aws apigateway get-resources --rest-api-id <id> --query 'items[].path' --output text        # expect /{proxy+} + /auth/github/{proxy+}
```

## Phase 6c — Broker Lambda code ✅ verified — REQUIRED for login

Terraform creates the `github-auth-broker` Lambda with a **503 placeholder zip**;
the real code is published by a push-triggered CI workflow that a fresh deploy
never fires. Publish it manually:

```bash
modules/gateway/scripts/deploy-broker.sh --env dev
```
Packages (linux/py3.12), uploads to S3, and `update-function-code`. Idempotent.

Verify:
```bash
aws lambda get-function-configuration --function-name bedrockgw-dev-github-auth-broker --query CodeSize  # ~16MB, not 223
# after the ALB second pass + broker deploy + GitHub wiring:
curl -s -o /dev/null -w "%{http_code}\n" "https://<apigw>/dev/auth/github/start"   # 302
```

## Phase 6d — Bootstrap the first admin ✅ verified — REQUIRED for login

A fresh deploy has **no rows in the `users` table**, so the onboarding gate
returns "request access" for **everyone** — including the seeded Cognito admin —
and nobody can approve anyone (`create_test_users=true` only creates the Cognito
user + "admins" group, not the DB rows). Seed the first admin's DB rows so they
become "registered" and can approve real users:

```bash
modules/gateway/scripts/bootstrap-admin.sh --env dev
```
Resolves the seeded admin's Cognito sub (from `adp/dev/gateway/test-admin-credentials`)
and runs the in-pod entrypoint `python -m src.admin.onboarding.bootstrap_admin`,
which reuses `approve_request` to atomically create org/tenant/dept/team/user +
the cognito identity (role `platform_admin`). It then sets the Cognito user's
`custom:role` + `custom:org_id` attributes (see below). Idempotent. For an
operator using their own SSO admin instead of the test admin, pass
`--email`/`--pool-id`/`--org`.

> **Two writes, not one — DB row *and* Cognito attributes.** The onboarding gate
> (`/access/status`) and backend admin powers come from the **DB row** + the
> Cognito `admins` group. But the frontend's org/role come from the access token's
> `custom:org_id` / `custom:role` claims, which the pre-token-generation Lambda
> copies from the **Cognito user attributes** — it never reads the DB. So seeding
> only the DB row leaves the admin "registered" but with an empty org/role in the
> UI. The script sets both; **log out and back in afterward** so a fresh token
> carries the claims.

> **The image must contain `bootstrap_admin.py`.** It runs *inside* the gateway
> pod, so the deployed `adp-gateway` image must include this module — i.e. build
> the gateway image (Phase 5) from a tree that has it. (If the pod predates it,
> rebuild + redeploy the image first.)

Verify:
```bash
# Log in as the admin (email/password), then:
curl -s -H "Authorization: Bearer <admin-jwt>" https://<apigw>/dev/admin/access/status   # {"status":"registered"}
```

## Agent Factory — Webhook-Ingress stack (ARC-free agent path) ✅ verified

This is the **GitHub-decoupled, ARC-free** way to run agents:
`GitHub webhook (@agent-developer mention / issue label) → API Gateway → Lambda
(HMAC + intent parse) → SQS FIFO → KEDA ScaledJob → agent-worker pod`. No
self-hosted runners, no in-repo workflow. GitHub is wired **after** deploy via a
single GitHub App that users install per-repo (see register step below).

One script does all three prerequisites (they belong together — skipping any
leaves a broken stack):

```bash
modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh --env dev
```

It runs, in order:
1. **Build the agent-worker image** → ECR `adp-agent-runtime` (CodeBuild). The
   KEDA ScaledJob runs `adp-agent-runtime:latest`; nothing else builds it and
   terraform never validates it, so skipping → `ImagePullBackOff` on the first
   real agent run.
2. **Package + upload the webhook Lambda zip** → `s3://<bucket>/lambda-artifacts/webhook-ingress/github.zip`
   (terraform reads it at PLAN time; apply fails without it).
3. **`terraform apply`** the stack (~60 resources; secrets auto-create as
   placeholders — no GitHub needed yet).

Flags: `--dry-run`, `--skip-image`, `--skip-lambda`, `--skip-terraform`. Idempotent.

### Verify
```bash
aws apigateway get-rest-apis --query 'items[?name==`adp-dev-webhook-ingress`].id' --output text
aws sqs list-queues --queue-name-prefix adp-dev-agent-submit --query 'QueueUrls'
for t in tenant-registry webhook-events rate-limits; do aws dynamodb describe-table --table-name adp-dev-$t --query 'Table.TableStatus' --output text; done
kubectl get scaledjobs -n adp-agents        # agent-scaledjob, READY=True
# Smoke test — an UNSIGNED POST returns 401 (HMAC rejection = path is live & correct):
ID=$(aws apigateway get-rest-apis --query 'items[?name==`adp-dev-webhook-ingress`].id' --output text)
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://${ID}.execute-api.us-east-1.amazonaws.com/dev/github" -d '{}'   # 401 = good
```

### Gotchas
- **`POST /github` → 401 on an unsigned/empty body is CORRECT** — the Lambda is
  rejecting a request with no valid HMAC signature. Confirms API GW→Lambda works.
- **All three prerequisites above are a single unit.** Two of them are plan-time
  hard deps (the Lambda zip) or silent runtime deps (the agent-runtime image —
  terraform never validates it, so a missing image only surfaces as
  `ImagePullBackOff` on the first real agent run). Don't treat the image build as
  optional.
- **KEDA ScaledJob namespace is `adp-agents`** (the README says
  `adp-gateway-agents` — doc drift).

### Then wire GitHub — `register-github-app.sh`
After the stack is up, run `register-github-app.sh` to make GitHub integration
live:
- Creates **one** GitHub App (`adp-agent-platform`), sets its webhook → this API
  Gateway, and populates the 3 placeholder secrets.
- **Visibility is operator-chosen** (default **private**; choose public only for
  cross-org multi-tenant).
- Users then install that one App on their repos via the UI "Link GitHub" flow
  (repo-admin only).

### End-to-end checklist — full ordered script sequence (working agent)
The complete stage-by-stage path, each step backed by a re-runnable script:
1. Phases 1–6 — bootstrap → preflight → platform → gateway infra → gateway backend → frontend (above)
2. `platform/scripts/wire-gateway-alb.sh --apply` — gateway second pass (Phase 6b)
3. `modules/gateway/scripts/deploy-broker.sh --env dev` — broker Lambda code (Phase 6c)
4. `modules/gateway/scripts/bootstrap-admin.sh --env dev` — first-admin DB rows (Phase 6d; REQUIRED for login)
5. `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh --env dev` — webhook stack
6. `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env dev [--client-secret <s>]` — create + wire the GitHub App
7. Install the App on a target repo (UI "Link GitHub" or `github.com/apps/<slug>/installations/new`)
8. `@agent-developer <task>` in an issue/PR comment → webhook → SQS → KEDA → agent-worker pod

## Phases 7–9 (full ARC path) — `deploy-all.sh` ⚠️ unverified here

`deploy-all.sh` runs bootstrap → preflight → platform infra → gateway infra →
backend image build (CodeBuild) → k8s deploy → frontend → agent factory → agent
gateway, including a two-pass gateway apply for the ALB. It does NOT deploy the
webhook-ingress stack or build the agent-runtime/broker artifacts — use the
stage-by-stage scripts above for the webhook agent path.

**Verify when done:**
```bash
aws eks describe-cluster --name adp-dev-eks-cluster --query 'cluster.status'   # ACTIVE
kubectl get nodes
kubectl get pods -n adp-gateway                                                # 2/2 Running (gateway scope)
CF=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text)
curl -s -o /dev/null -w "%{http_code}\n" "https://${CF}/"                      # 200
```

---

## When it breaks

Items marked *(fixed on `main`)* only bite on older checkouts.

| Symptom | Cause | Fix |
|---------|-------|-----|
| `sed: -e: No such file or directory` *(fixed on `main`)* | macOS BSD sed vs GNU `sed -i -e` | Update to latest `main`; or re-run the substitution with `sed -i ''` (see Phase 1) |
| `terraform init`: bucket doesn't exist | Bootstrap didn't run / wrong account | Run `bootstrap.sh` with the right profile |
| `terraform init`: `ACCOUNT_ID` in tfvars *(fixed on `main`)* | macOS sed skipped the rewrite | Update to latest `main`; or run the Phase 1 fix snippet |
| `terraform init`: AccessDenied on a *different* account's bucket | Old account id baked in tfvars from a prior run | Re-run `bootstrap.sh` (or the fix snippet) for the current account |
| Preflight: CodeBuild/Bedrock "cannot list" warnings *(fixed on `main`)* | invalid `--max-results 1` in the check | Update to latest `main`; or verify manually (see Phase 2) |
| CI `Platform Infra Plan` fails on `adp-terraform-state-ACCOUNT_ID` *(fixed on `main`)* | workflow didn't substitute the account at init | Update to latest `main` (PR #1210) |
| `CreateSecurityGroup ... Character sets beyond ASCII` *(fixed on `main`)* | non-ASCII char in an IaC string | Update to latest `main`; keep IaC descriptions ASCII-only |
| `terraform apply` "succeeded" but didn't | piped to `tail`, got tail's exit code | Capture real code: `apply > log 2>&1; echo $?` or `${PIPESTATUS[0]}` |
| `namespaces is forbidden` on first apply | EKS cluster-admin entry not propagated yet | Re-run `terraform apply` (idempotent) |
| Creds expired mid-deploy | Temporary STS token timed out | Refresh creds, re-`export AWS_PROFILE`, re-run (phases are idempotent) |
| EKS nodes don't appear | Auto Mode takes 3–5 min | Wait, `kubectl get nodes` again |
| CloudFront 502/504 | ALB not provisioned by Ingress yet | Wait 2–3 min; `kubectl get ingress -n adp-gateway` |

---

## Teardown

```bash
./platform/scripts/deploy-all.sh --destroy      # reverse order: agent-context → agent-factory → gateway → platform
./platform/scripts/bootstrap-destroy.sh         # state backend, separate, typed confirmation
```

Survives by design: state backend (until `bootstrap-destroy.sh`), GitHub App
secrets, the GitHub Apps themselves (delete manually in org settings).
