# Deployment phase and troubleshooting reference

Use the [deployment quickstart](deploy-quickstart.md) for a normal install or
upgrade. This reference preserves detailed phase commands, verification probes
and findings from earlier deployments for diagnosis and manual recovery. Do not
run every manual phase after the automated launcher has completed. Historical
verification dates and fixed-issue notes below are not evidence for a new release.

For the exhaustive resource-by-resource reference, see
[`deployment-manifest.md`](./deployment-manifest.md). For a real run's log of what
broke and how it was fixed, see the `deployment_learning_<accountID>.md` at the
repo root.

> **Status of this guide:** Phases 1–6c (Bootstrap → Preflight → Platform infra →
> Gateway infra → Gateway backend → Frontend → ALB second-pass → Broker) plus the
> webhook agent path are **verified end-to-end against a real run** (account
> `000000000229`, macOS, 2026-06-06/07). Each step is backed by a re-runnable
> script. Bugs
> found during that run are **fixed on `main`** across PRs #1210 (sed/preflight/CI),
> #1218 (gateway count arg + Lambda-layer auto-build + gateway CI), and #1221
> (VPC-endpoint SG for IRSA) — if you're on latest `main`, the gotchas called out
> below no longer bite; they're kept to explain behavior and for older checkouts.
> Phases 6–9 are transcribed from the full guide and not yet re-verified here
> (marked ⚠️ *unverified*). Agent Factory phases (CLAUDE.md 4–5) were not exercised
> in the gateway-only run.

---

Persona model mapping is enabled by default in full deployments. See
[model mapping deployment and verification](./persona-model-mapping.md) for
model availability, environment overrides and acceptance checks.

Orchestration is enabled by default. To disable it for an environment, set
`orchestration_engine_enabled = false` in its gateway Terraform inputs and set
SSM `/adp/<env>/gateway/feature-orchestration-engine` to `false`, then run the
upgrade deployment. Set both to `true` to re-enable it. The browser waits for
confirmed server capabilities before displaying orchestration controls.

## The shape of a deploy

A deploy is a sequence of **stage-by-stage scripts** (each idempotent, each one
phase below). GitHub is NOT set up upfront — for the webhook agent path, all
GitHub wiring happens at the **end** (Phase 8), after the infrastructure it
points at exists. The infra phases:

```bash
export AWS_PROFILE=<profile> AWS_REGION=us-east-1   # the account everything keys off
./platform/scripts/bootstrap.sh                     # Phase 1
./platform/scripts/preflight-check.sh               # Phase 2
# Phase 3–5: terraform applies + backend image build (see each phase below)
./modules/gateway/scripts/deploy-frontend.sh --env dev   # Phase 6 (build/sync SPA + CFN template)
./platform/scripts/wire-gateway-alb.sh --apply           # Phase 6b (gateway second pass)
./modules/gateway/scripts/deploy-broker.sh --env dev     # Phase 6c (login)
./modules/gateway/scripts/bootstrap-admin.sh --env dev   # Phase 6d (seed first admin)
# Agent execution (required for a full deployment):
./modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh --env dev   # Phase 7
./modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env dev  # Phase 9 (or use the UI — see Phase 9 below)
```

`deploy-all.sh` chains the infrastructure, gateway, broker, webhook/runtime,
agent-factory, and frontend stages. GitHub App setup follows deployment. The
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

The default full command installs the shared platform, gateway and agent stack.
It does not install domain apps. Install Superplane through
`modules/domain-apps/superplane/deploy.sh` or its module workflows, and Cyber's
sandbox through `modules/domain-apps/cyber/scripts/deploy.sh`. A hosted Cyber worker
integration is installed separately through
`modules/domain-apps/cyber/scripts/deploy-hosted-integration.sh`, then wired to
the shared worker by selecting `enabled_domain_integrations = ["cyber"]` in the
webhook update settings. Their
image build jobs are created by each domain app's own Terraform root. A basic
platform deployment does not create them. Upgrading an older installation whose
domain image jobs still live in platform state will show those jobs as removals;
review that saved plan and migrate installed apps to their own state before
applying. `ADP_ENABLED_DOMAIN_APPS` is retired.

Older webhook states that own `module.cyber` also need a reviewed state migration
before enabling the new Cyber hosted-integration root. A base platform update
does not create Cyber resources or read its module state.

If you just want to see the platform work with the least setup,
**`--gateway-only`** is simplest — no GitHub involvement at all.

> **No upfront GitHub setup.** Older docs had a "Phase 0" that ran `setup-org.sh`
> + `create-github-apps.sh` first. That was the legacy **ARC** track (3 org-owned
> apps, repo push). It's removed here: the webhook agent path wires GitHub at the
> **end** (Phase 9 — UI flow or `register-github-app.sh` CLI fallback), after the
> infra it points at exists. The only thing you need before Phase 1 is the right
> **AWS profile** (above) — `config/deployment.yml` is optional (account resolves
> from the profile if absent).

> **Updating an existing deployment?** Use `deploy-all.sh --update` instead of
> re-running the default fresh-deploy path. Update mode discovers installed
> modules, includes the required agent factory if missing, preserves existing
> GitHub integration, plan-gates Terraform, and
> runs migrations on Ready replicas of the intended SHA-tagged release. See
> [`platform_upgrades.md`](./platform_upgrades.md) for the current procedure
> and the narrowly allowed deployment replacements, or `deploy-all.sh --help`
> for usage.

Repeated `--update` runs reuse an existing full-SHA image tag for the gateway,
agent runtime, agent gateway, or chat agent only when its ECR repository has
immutable tags. The script still resolves the tag to its digest and runs all
remaining Terraform, migration, rollout, and verification gates. A missing
tag or a new source commit triggers CodeBuild as usual.

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
Creates VPC, EKS (Auto Mode, v1.35), ECR repos, IAM, CodeBuild projects,
security-scans bucket, and Bedrock invocation logging. ~10–15 min.

Bedrock Runtime invocation logging defaults to CloudWatch and an encrypted S3
destination, including large request/response bodies, with 30-day retention.
The setting applies to the entire account/region and must have one Terraform
owner. For shared accounts, existing configurations, disable/retention options
and delivery verification, follow [Bedrock invocation logging](bedrock-invocation-logging.md).

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

### ⚠️ Fresh-account Lambda concurrency quota (Issue #2910)

Gateway lambdas reserve 97 total concurrent executions (authorizer 50, pre-token
20, pre-signup 10, broker 10, usage-tracker 5, pricing-refresh 2). AWS requires
the account's **unreserved** pool to stay ≥ 100 after all reservations, so
`UnreservedConcurrentExecutions` must be ≥ 197 at apply time. Fresh accounts
default to a 1000 total quota (`L-B99A9384`) with the full pool unreserved
(fine), but two situations pin the unreserved pool low even at a 1000 total quota:
- new/restricted accounts with a lower `L-B99A9384` quota; and
- **SCP-locked or shared-pool accounts** where another reserved lambda consumes
  most of the total quota (e.g. Workshop-Studio sandbox accounts run a
  `WSConcurrencyCurtailer` function that reserves ~890, pinning unreserved at the
  100 floor). Here a Service Quotas increase is often SCP-denied, so Option B is
  the only path.

`preflight-check.sh` now reads `UnreservedConcurrentExecutions` (not the total
quota) and warns if that pool is too low. If it warns:
- **Option A**: request a Service Quotas increase for `L-B99A9384` (only helps if
  the *total* quota is the constraint; SCP-locked accounts will get AccessDenied).
- **Option B** (always works): set `enable_lambda_reserved_concurrency = false` in
  `environments/dev/modules/gateway.tfvars`. Lambdas will run unreserved
  (no throttle protection) until the pool frees up; then flip back to `true`.

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

```bash
./modules/gateway/scripts/deploy-frontend.sh --env dev
```

One idempotent script (the manual equivalent of `gateway-deploy.yml`'s frontend
job). It:
1. **Builds with the FULL `VITE_*` env resolved from SSM** — not just
   `VITE_API_URL`. Vite vars are baked into the bundle at build time; building
   with only `VITE_API_URL` ships a broken app (*"GitHub sign-in is not
   configured"* + Cognito unconfigured). All values come from SSM params Phase 4
   created.
2. **Syncs `dist/` → the frontend bucket**, excluding `cfn-templates/*` so the
   `--delete` doesn't wipe the template.
3. **Uploads the CloudFormation role template** (`cfn-templates/aws_role_v1.yaml`)
   — REQUIRED for **Settings → Add AWS account**. The gateway pre-signs a GET for
   it; without it the flow fails *"S3 error: The specified key does not exist."*
   (Pairs with the gateway-infra grant — `s3:ListBucket`/`GetObject` on
   `cfn-templates/*`, applied in Phase 4.)
4. **Invalidates CloudFront.**

Flags: `--dry-run`, `--skip-build` (deploy an existing `dist/`), `--skip-template`.
Notes: `VITE_AGENT_WS_URL` may be empty (the agent-gateway WebSocket isn't part of
the webhook path) — the app tolerates it. Node 24 builds fine (repo says ≥ 22).
If you change broker/Cognito values, re-run the script (they're compiled in, not
read at runtime).

Verify:
```bash
CF=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text)
curl -s -o /dev/null -w "%{http_code}\n" "https://${CF}/"        # 200
curl -s "https://${CF}/" | grep -o "<title>[^<]*</title>"       # <title>Agentic Developer Platform</title>
# API probe: assert the JSON BODY, never the status code alone — without the
# VPC origin, /api/* falls to the S3 SPA fallback which also returns 200
# (HTML). Masked both 608-deploy incidents (#3085).
curl -s "https://${CF}/api/health"                               # {"status":"healthy"}
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

> **KMS grant for webhook secrets (Issue #3789):** The webhook-secrets CMK
> (`alias/adp-<env>-webhook-secrets`) is now owned by **platform infra** (Phase 2),
> so it always exists before gateway applies. The gateway KMS grant is
> unconditional — no `enable_webhook_secrets_kms_grant` flag needed. For existing
> environments being migrated, run `platform/scripts/migrate-webhook-kms.sh` to
> move the CMK state from webhook-ingress to platform (see #3789).

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

## Phase 6d — Bootstrap the first admin — REQUIRED for login

A fresh deploy has no user rows, so the onboarding gate returns "request access"
with nobody able to approve it. Select the operator email before installation
(see [administrator setup](bootstrap-administrator.md)); the full installer runs
this step automatically after the gateway is healthy. For a manual phase run:

```bash
modules/gateway/scripts/bootstrap-admin.sh --env dev --email 'operator@your-company.example'
```

The script discovers the Cognito pool through SSM, invites the operator if the
user does not exist, and runs the in-pod database bootstrap. Cognito sends a
temporary password that must be changed on first sign-in. No shared permanent
password or operator credential secret is created. Existing users keep their
password; retries are idempotent when the requested database role and org match.
`--pool-id` and `--org` can override discovery and the default `platform-admin` org.
Dev/test fixtures require explicit `--use-test-admin` and `create_test_users=true`.

The script seeds the database records and verifies the Cognito `custom:role` and
`custom:org_id` attributes. The pre-token-generation Lambda copies these into
fresh tokens; sign out and back in after bootstrap. A partial database-only run
must not be considered complete.

> **The image must contain `bootstrap_admin.py`.** It runs *inside* the gateway
> pod, so the deployed `adp-gateway` image must include this module — i.e. build
> the gateway image (Phase 5) from a tree that has it. (If the pod predates it,
> rebuild + redeploy the image first.)

Verify:
```bash
# Log in as the admin (email/password), then:
curl -s -H "Authorization: Bearer <admin-jwt>" https://<apigw>/dev/admin/access/status   # {"status":"registered"}

# CRITICAL: verify Cognito attributes are set (the script does this automatically,
# but confirm manually if you ran an older version or suspect a partial run):
POOL_ID=$(aws ssm get-parameter --name /adp/dev/gateway/cognito-user-pool-id --query Parameter.Value --output text)
ADMIN_EMAIL="<admin-email>"  # the operator email selected for bootstrap
aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$ADMIN_EMAIL" \
  --query 'UserAttributes[?Name==`custom:role` || Name==`custom:org_id`]'
# Expected: custom:role=platform_admin, custom:org_id=platform-admin (or your --org value)
# If MISSING: the admin will log in but the UI will NOT show platform-admin views
# (e.g. GitHub App "Set up" CTA hidden). Re-run bootstrap-admin.sh to fix.
```

> **⚠️ Pipeline gap:** The ADP-managed pipeline (`gateway-deploy.yml`) does **NOT**
> run `bootstrap-admin.sh`. On a pipeline-driven deploy, an operator must run this
> script manually after Phase 6c (gateway backend is healthy). If this step is
> skipped or partially completes, the first admin will have DB access but the
> frontend will not recognize them as `platform_admin` — the pre-token Lambda reads
> Cognito attributes, not the DB. The script now verifies attributes on completion
> and fails loudly if they're missing; older versions do not — use the manual check
> above.

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
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://${ID}.gateway-14.example.com/dev/github" -d '{}'   # 401 = good
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

### ⚠️ Two account-level blockers that make the agent hang silently after "Session initialized"

Both were hit on a real run (account `000000000229`). The agent clones the repo,
posts a live status comment, reaches `Session initialized`, then **hangs with no
further updates** — its first Bedrock call never completes. Neither surfaces a
clear error in the live GitHub comment, so check for these directly. (Both are
fixed in IaC by PR #1304; the notes below are for accounts deployed before it or
diagnosing the symptom.)

1. **`execute-api` VPC interface endpoint hijacks DNS → blanket `403
   ForbiddenException`.** If `platform/infra` created an `execute-api` interface
   VPC endpoint with `private_dns_enabled=true` (it did between #1177 and #1304),
   it hijacks **all** `*.execute-api.<region>.amazonaws.com` DNS inside the VPC to
   the endpoint's private ENIs. That endpoint only serves **PRIVATE** API Gateway
   APIs, but `bedrockgw-dev-api` (the `/agent` path) and the webhook API are both
   **REGIONAL/public** — so the worker's signed `/agent` call resolves to the
   VPCE, matches no private API, and gets `403 ForbiddenException` for *every*
   route. Both APIs are public by design (the worker **and** developers' laptops
   hit the bedrock API; GitHub hits the webhook API), so there is no private API
   to serve — the endpoint must not exist.
   - **Diagnose:** from a pod in `adp-agents`, resolve the API host. Private
     `10.x` IPs = hijacked (broken); public IPs = good.
     ```bash
     # broken: resolves to the VPCE private ENIs
     getent hosts <bedrock-api-id>.gateway-14.example.com
     # confirm the culprit endpoint exists:
     aws ec2 describe-vpc-endpoints \
       --filters Name=service-name,Values=com.amazonaws.us-east-1.execute-api \
       --query 'VpcEndpoints[].{id:VpcEndpointId,privateDns:PrivateDnsEnabled}'
     ```
   - **Fix:** on latest `main` (#1304) the endpoint is gone — re-apply
     `platform/infra`. To unblock an already-deployed account immediately:
     `aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <vpce-id>` (DNS reverts to
     the public edge within a couple of minutes). A signed `/dev/agent/health`
     then returns **200** instead of `Forbidden`.

2. **Bedrock model access / wrong inference-profile prefix.** The agent invokes
   `us.anthropic.claude-opus-4-6-v1` (#1304; older images used
   `global.anthropic.claude-opus-4-6-v1`). On a fresh account:
   - **Model access must be enabled** for Claude Opus 4.6 — run
     `platform/scripts/enable-bedrock-models.sh` (CLI-only; accepts the
     marketplace agreement the API error names — the old "console-only" note
     is outdated, and both deploy tracks now run this automatically). Until
     then, `InvokeModel` returns `AccessDeniedException: ...
     aws-marketplace:Subscribe ...`. Takes ~2 min to propagate after enabling.
   - **Use the `us.` profile, not `global.`** — the `global.` cross-region profile
     is not enabled on every account (919 returns `ValidationException: invalid
     model identifier`); `us.anthropic.claude-opus-4-6-v1` works on every account
     tested. The streaming path makes this especially nasty: the gateway returns
     HTTP **200** with an empty `text/event-stream`, so the SDK waits forever with
     no error — exactly the silent hang.
   - **Diagnose (source of truth is `invoke`, not `list-inference-profiles`):**
     ```bash
     aws bedrock-runtime invoke-model --model-id us.anthropic.claude-opus-4-6-v1 \
       --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' \
       --cli-binary-format raw-in-base64-out /dev/stdout   # expect a real JSON message
     ```

### ⚠️ Slow first agent (1-2 min to first comment) on small/fresh clusters

On a cluster running scale-from-zero with few nodes, a summoned agent waits for
**both** a cold node to be provisioned (~60-90s on EKS Auto Mode / Karpenter)
**and** the ~650 MB agent image to be pulled (~30s) before it can run — so the
first live comment lands in 1-2 min, vs 10-15s on a warm multi-node cluster.
Confirmed via pod events: `Pulled image ... in 31s` on a cold node vs `174ms`
when cached.

**Fix — two complementary mechanisms in `warm-pool.tf`** (mirrors how example-profile stays
fast: warm nodes + a `chat-agent-image-prepull` DaemonSet):

1. **Warm pool** (`agent_warm_pool_replicas`, default `1`) — N "balloon" pods run
   the agent image at **negative priority**, holding warm nodes. A summoned agent
   (priority 0) preempts a balloon and starts on that node instantly; the balloon
   reschedules and re-warms a node for the next summon. Removes the **cold-node
   boot** delay for up to N parallel agents. Each replica holds one node 24/7
   (cost trade-off); `0` disables.
2. **Image pre-pull DaemonSet** (`agent_image_prepull_enabled`, default `true`) —
   one tiny pod per node keeps the ~650 MB agent image cached on **every** node,
   so the ~30s pull is gone regardless of which node an agent lands on — covering
   parallel agents beyond the warm-pool count and agents scheduled onto other
   nodes. Tiny footprint (10m cpu / 32Mi per node).

Verify:
```bash
kubectl get deploy agent-warm-pool -n adp-agents               # READY = replicas
kubectl get daemonset agent-image-prepull -n adp-agents        # READY = node count
# After a summon, the agent pod's events should show NO new node + image "Pulled ... in <1s"
# (confirmed live on 919: 168ms cached vs 31s cold).
```

### Then wire GitHub — UI flow (primary) or `register-github-app.sh` (fallback)

After the webhook-ingress stack is up, wire GitHub so agent mentions trigger
webhook events. This creates the GitHub App (`adp-agent-platform`) people install
on their repos, points its webhook at this deployment's API Gateway, and stores
the App's credentials in **this deployment's** Secrets Manager. **One App per
deployment.**

#### Primary path: UI flow (recommended)

The **Phase-6d bootstrap `platform_admin`** performs this step:

1. Log in as the `platform_admin` (the admin seeded in Phase 6d).
2. Navigate to **Settings → Connections → "Set up GitHub App"**.
3. Click through the GitHub manifest flow — GitHub prompts you to name the app
   and select the org/account to own it.
4. On success the UI stores App ID + private key + webhook secret in Secrets
   Manager and wires them into the platform automatically.
5. Install the App on the target repo(s) when prompted.

> **Org-owner vs user-owned App.** If the `platform_admin` is an org owner on
> GitHub, the manifest flow creates an **org-owned** App (visible at
> `github.com/organizations/<org>/settings/apps`). Non-owners get a user-owned
> App. Both work; org-owned is preferred for production / shared deployments.

#### Fallback: CLI (`register-github-app.sh`)

For headless / CI environments where no browser session is available, use the
script directly:

```bash
# 1. Credentials for the account you deployed INTO (cross-account: assume the
#    customer role first). Confirm — the App secrets are written to THIS account.
export AWS_PROFILE=<your-profile>          # or assume into the customer account
export AWS_REGION=us-east-1
aws sts get-caller-identity --query Account --output text    # MUST be your deploy account

# 2. gh must be logged in as someone who can create Apps in <your-org> (admin:org).
gh auth status

# 3. Register (pass YOUR GitHub org as the first arg):
./modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <your-org> --env dev
#    No org-OWNER rights on <your-org>? Create a personal App instead:
#    ./...register-github-app.sh <your-org> --env dev --owner-type user --repo <your-org>/<repo>
```

> **Org-owner vs user-owned App (CLI path).** Creating an **org-owned** App (the
> default) requires **org-owner** rights on `<your-org>` — many people don't have
> that. If you don't, pass **`--owner-type user`** to create the App under your
> own account instead (any user can; install it on repos you admin). For a shared
> team deployment, have an org owner run it with the default `--owner-type org`.
> Use `--repo <owner/name>` to set the install-target repo in the printed URL
> (defaults to `<your-org>/adp`).

The script will:
- prompt **private vs public** (choose **private** unless you need cross-org
  multi-tenant install);
- **open a browser** to `https://github.com/organizations/<your-org>/settings/apps/new`
  with the name, webhook URL, and permissions pre-filled → click **Create GitHub
  App** → **Generate a private key** (downloads a `.pem`) → note the **App ID**
  (+ client secret, shown once);
- prompt for the **App ID** and **.pem path** (or pass `--app-id` / `--pem-path`
  / `--client-secret` as flags to skip the prompts), then store them in Secrets
  Manager + wire the gateway.

#### After wiring (either path)

Install the App on the repo(s) you'll trigger agents from (the UI flow prompts
for this; for CLI use `https://github.com/apps/<app-slug>/installations/new` —
repo-admin only).

**Verify:** run `./platform/scripts/verify-github-wiring.sh --installation-id <id>
--repo <owner/name> --issue <n>` — it checks the secrets and identity rows, then
comments `@agent-developer say hello` and asserts a **reply** arrives. A pod
spawning is not sufficient (it spawns and crash-loops when the per-tenant secret
is missing); see [Phase 9 → Verify](#verify-1) for what each check proves.

### End-to-end checklist — full ordered script sequence (working agent)
The complete stage-by-stage path, each step backed by a re-runnable script:
1. Phases 1–6 — bootstrap → preflight → platform → gateway infra → gateway backend → frontend (above)
2. `platform/scripts/wire-gateway-alb.sh --apply` — gateway second pass (Phase 6b)
3. `modules/gateway/scripts/deploy-broker.sh --env dev` — broker Lambda code (Phase 6c)
4. `modules/gateway/scripts/bootstrap-admin.sh --env dev` — first-admin DB rows (Phase 6d; REQUIRED for login)
5. `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh --env dev` — webhook stack
6. **GitHub App** — UI (primary): log in as `platform_admin` → Settings → Connections → "Set up GitHub App" → manifest flow. CLI fallback: `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env dev [--client-secret <s>]`
7. Install the App on the target repo(s) when prompted (or `github.com/apps/<slug>/installations/new`)
8. `platform/scripts/enable-bedrock-models.sh` — Bedrock marketplace agreements
   (one-time per account, CLI-only, idempotent; also runs automatically inside
   deploy-all.sh and platform-infra-apply.yml). Without it the agent hangs
   silently after "Session initialized" or ends "no changes needed" with
   $0.0000 / 1 turn — see the gotcha above.
9. `@agent-developer <task>` in an issue/PR comment → webhook → SQS → KEDA → agent-worker pod

## Phase 7 — Webhook ingress + warm pool

`webhook-ingress/scripts/deploy-webhook-ingress.sh --env dev` builds the
agent-runtime image, packages the webhook Lambda zip, and terraform-applies the
stack (incl. `warm-pool.tf` balloon Deployment + image-prepull DaemonSet for
~10-15s agent starts). **Verify**: webhook API GW deployed; SQS FIFO
`adp-dev-agent-submit.fifo`; KEDA pods Running; ScaledJob `agent-scaledjob` in
`adp-agents`; `agent-warm-pool` + `agent-image-prepull` READY. Smoke: unsigned
`POST /dev/github` → 401 (HMAC reject = path live).

## Phase 8 — Bedrock model access and first-run verification

`./deploy.sh` and `deploy-all.sh` prepare Marketplace agreements and Anthropic
first-use registration when needed, verify all access states for the actual
runtime defaults, and perform bounded default-model invocations before reporting
success. Existing account or organization authorization is reused.

For an unregistered account, supply your organization's actual first-use JSON:

```bash
./deploy.sh --anthropic-use-case /secure/path/anthropic-use-case.json
```

Direct `deploy-all.sh` and automated helper invocations accept the same file
through `ADP_BEDROCK_USE_CASE_FILE`. See
[Bedrock readiness during deployment](./bedrock-first-run.md) for permissions,
form fields, standalone checks, invocation costs and organization restrictions.
No per-user persona mapping is required for these default-model checks.


## Phase 9 — GitHub App ⚠️ HUMAN browser step

The GitHub App connects the platform to your GitHub org so agent mentions
trigger webhook events. The **Phase-6d bootstrap `platform_admin`** is the actor
for this step (they have the `platform_admin` role required by Settings →
Connections).

### Primary path: UI flow (recommended)

1. Log in as the `platform_admin` (the admin seeded in Phase 6d).
2. Navigate to **Settings → Connections → "Set up GitHub App"**.
3. Click through the GitHub manifest flow — GitHub prompts you to name the app
   and select the org/account to own it.
4. On success the UI stores App ID + private key + webhook secret in Secrets
   Manager and wires them into the platform automatically.
5. Install the App on the target repo(s) when prompted (or later via
   `https://github.com/apps/<app-slug>/installations/new`).

> **Org-owner vs. user-owned:** If the `platform_admin` is not an org owner on
> GitHub, the manifest flow creates a *user-owned* app. Org owners get an
> org-owned app (visible at `github.com/organizations/<org>/settings/apps`).
> Both work; org-owned is preferred for production.

### Fallback: CLI (`register-github-app.sh`)

For headless / CI environments where no browser session is available:

```bash
modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env dev [--client-secret <s>]
```

The script creates the App, stores creds in Secrets Manager, and calls
`wire-github-app.sh`. Pass `--client-secret` to also wire GitHub login in the
same run. See the script's `--help` for all non-interactive flags (`--app-id`,
`--pem-path`, `--visibility`).

### Verify

**Run this after installing the App on at least one repo** — several of the
artifacts it checks are created by the `installation` webhook, so it will
legitimately fail if run before the install completes.

```bash
# Wiring only (read-only, no side effects):
./platform/scripts/verify-github-wiring.sh --installation-id <id>

# Wiring + a real end-to-end round trip (posts a comment, spends Bedrock tokens):
./platform/scripts/verify-github-wiring.sh --installation-id <id> \
  --repo <owner/name> --issue <n>
```

Get `<id>` from <https://github.com/settings/installations> (or the URL you
landed on after installing). Exit 0 = pass, 1 = a hard check failed.

What it asserts, and why each one matters:

| # | Check | Severity |
|---|---|---|
| 1 | Platform App secrets `adp/<env>/github-app/adp-agent-platform-{id,key}` exist, App ID numeric, key PEM-armoured | **HARD** |
| 2 | Forward identity row `github_installation_id` → `org_id` in `adp-<env>-identity-index` (also *derives* the tenant ID for step 4) | **HARD** |
| 3 | Reverse row `org_installation` → `installation_id` | WARN |
| 4 | Per-tenant secret `adp/<env>/tenants/<tenant>/github-app` exists **and parses** (`app_id` + PEM `private_key`) | **HARD** |
| 5 | `@agent-developer say hello` receives an actual **reply comment** | HARD when run |

> **Why the script instead of a couple of inline commands.** A deployment can
> pass "the App ID secret is non-empty" and "a worker pod spawned" while dispatch
> is 100% broken — that is exactly what happened in the Acme PoV. The worker
> hard-requires the **per-tenant** secret (step 4) with no fallback; when it is
> missing, pods spawn, die in bootstrap, and crash-loop. Step 5 asserts a reply
> *landed*, because a pod spawning proves only that the webhook path works, not
> that the agent can run. Step 5 also ignores the `<!-- adp-run: -->` "started"
> comment the worker posts before it does any real work — otherwise a Phase 8
> Bedrock failure (the gotcha immediately above) would still look like a pass.

> **Non-dev environments:** the block is pinned to `dev` on purpose. The worker's
> vault client keys the secret path off `ADP_ENV`, which is currently injected
> nowhere and therefore resolves `dev` regardless of the deployment's real
> environment. On a non-dev deploy, verify against the path the worker *actually*
> reads. Tracked separately as a code fix (#4042).

On failure the script prints the discriminator log searches that tell the failure
modes apart — the durable webhook-Lambda queries in
`/aws/lambda/adp-<env>-github-webhook` (`Auto-registered installation_id=`,
`Auto-provision:`) plus the `kubectl logs -n adp-agents` search for
`vault_fetch`, which must be run **while the pod still exists**.

## Phase 10 — End-to-end smoke test

Run the frontend/backend/Cognito probes below, then confirm an
`@agent-developer <task>` comment on the linked repo spawns an agent-worker pod
and opens a PR (proves webhook → SQS → KEDA → worker → gateway → Bedrock).

> **Agent Context (Code Intelligence Layer) is a separate, optional follow-on** —
> not part of the phases above. Deploy it after the platform is up with
> `deploy-all.sh --agent-context-only` (or the `agent-context-*` workflows on the
> ADP-managed track). It provisions context-mcp, ingestion, Neptune/GraphRAG,
> and the MCP verbs (search/understand/impact/browse/secure/…). It has its own
> post-deploy activation steps (image build, migrations, vuln-scan cron, Neptune
> wiring) — see the agent-context module docs.

> **Hosted cross-account execution is unavailable.** Do not pass
> `customer_account_id`, `customer_aws_label`, or `customer_user_id` to the
> workflows. The shared config action rejects those inputs before plan, apply,
> or destroy. Use this self-managed sequence with customer-controlled temporary
> credentials. See [ADP-Managed Deploy](./adp-managed-deploy.md) for status.

**`deploy-all.sh` shortcut:** chains bootstrap → preflight → platform infra →
gateway infra → backend build → k8s deploy → ALB wire → frontend → broker (6c) →
admin bootstrap (6d) → agent-factory → webhook-ingress (7), i.e. Phases 1–7
(steps 1–10/11 in the script). The only remaining manual step is GitHub App
wiring (Phase 8/9) — the script prints next-steps guidance at the end.

**Verify when done:**
```bash
aws eks describe-cluster --name adp-dev-eks-cluster --query 'cluster.status'   # ACTIVE
kubectl get nodes
kubectl get pods -n adp-gateway                                                # 2/2 Running (gateway scope)
CF=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text)
curl -s -o /dev/null -w "%{http_code}\n" "https://${CF}/"                      # 200
curl -s "https://${CF}/api/health"   # {"status":"healthy"} — assert the BODY;
                                     # without the VPC origin, /api/* hits the
                                     # S3 SPA fallback and still returns 200 (#3085)
```

---

## Phase 12 — Dev Box (optional module)

The EC2 developer desktop (`modules/user-services/dev-box/`) is **not part of the
core platform**. A fresh environment without it is exactly today's platform: no new
inputs, no new failures, empty `BG_DEV_BOX_*` values in the gateway and no desktop
option in the UI. It joins a deployment in one of three ways:

| How | Effect |
|---|---|
| `deploy-all.sh --dev-box on` (or `ADP_DEV_BOX=on`; `deploy.sh` passes both through) | Step 12 runs after the agent factory and chat (Steps 10 and 10c) |
| `environments/<env>/modules/dev-box.tfvars` with `stage_enabled = true` | same, by default for that environment (dev does this); `--dev-box off` overrides |
| `deploy-all.sh --dev-box-only` | platform (Steps 1–2) + Step 12 only — add the module to an existing environment |

Step 12 is one call into the module's own entrypoint,
`modules/user-services/dev-box/scripts/deploy-dev-box.sh --env <env> --region <region> [--update]`,
which can also be run standalone. It needs the gateway already deployed (it reads
`bedrockgateway-config` and execs into `deploy/bedrockgateway`), the two tfvars
files (`dev-box.tfvars`, `dev-box-platform.tfvars`) and AWS + kubectl access to the
platform account, and it refuses with a named input when any is missing. What it does:

1. **Platform prerequisites** (`infra/platform`, state key
   `<env>/modules/dev-box-platform/terraform.tfstate`): workspace KMS key, `ssm` /
   `ssmmessages` / `ec2messages` interface endpoints + their security group in the
   platform VPC (the VPC has ECR/S3 endpoints only), and the AMI CodeBuild project
   `adp-<env>-dev-box-image`. Handed to the next steps through SSM
   `/adp/<env>/dev-box/{kms-key-arn,ssm-endpoint-security-group-id,image-builder-project}`.
2. **Image**: reuses `/adp/<env>/dev-box/image-id` or runs
   `scripts/build-dev-box-image.sh` (Packer in CodeBuild, Session Manager transport,
   private build instance) to publish it. Base image only by default; a Crew image
   needs the approved bundle + profile (`--crew-bundle --crew-manifest --profile-revision`),
   which `scripts/release-crew-image.sh --env <env> --crew-model-id <id> --apply` produces
   from the pins in `image/crew-bundle.pins` (`scripts/build-crew-bundle.sh`, CodeBuild
   `image/bundle-buildspec.yml`), builds, reads back against the AMI tags, pins in SSM
   `image-id` / `image-revision` / `profile-revision` / `crew-model-id` + `dev-box.tfvars`
   `crew_model_id`, and rolls out with `deploy-dev-box.sh --skip-image --skip-verify`
   (README "Crew image pipeline"; validation lines in `deployment-manifest.md`).
3. **Foundation** (`infra`, key `<env>/modules/dev-box/terraform.tfstate`): guest and
   fenced controller roles, launch template, the controller runtime IRSA role, and
   SSM `/adp/<env>/dev-box/*` (deployment id, deployment-wide bounds, role ARNs).
4. **Release**: the same manifest the ADP-managed workflow produces
   (`scripts/self_managed_release.py` → `release.create_manifest`), read back against
   the live AMI tags, network and IAM by `runtime.py`; receipt kept under
   `.adp-dev-box/<env>/release/`.
5. **Gateway + controller**: patches `BG_DEV_BOX_DEPLOYMENT_ID` and the five
   `BG_DEV_BOX_MAX_DEPLOYMENT_*` bounds into the live ConfigMap (both renderers
   re-render the same values from SSM on every deploy) with a gateway rollout, then
   applies `k8s/controller.yaml`: Deployment `dev-box-controller` in `adp-gateway`,
   gateway image, `controller/*.py` + manifest/outputs from ConfigMaps, IRSA =
   `adp-<env>-dev-box-controller-runtime`.
6. **Seed**: `kubectl exec deploy/bedrockgateway -- python -m src.cloud_desktop.seed`
   upserts the `dev_box_policies` row and the approved `dev_box_template_versions`
   row from Terraform's `policy_spec` output and adds `ec2-instance` to the
   organization's `crew.allowed_targets` (never raw SQL). Idempotent.
7. **Verify**: `scripts/verify-dev-box.py` when an operator token exists (`ADP_TOKEN`
   or `adp login`): creates the operator's box via the API, waits for `running`,
   checks the instance is `Online` in SSM, reports Crew readiness, releases it.
   Exit codes 0/1/2/3/4 are documented in the script header.

**Verify**: `kubectl -n adp-gateway get deploy dev-box-controller` Ready 1/1;
`aws ssm get-parameter --name /adp/<env>/dev-box/deployment-id`;
`aws ssm get-parameter --name /adp/<env>/dev-box/ssh-grant-role-arn` and the ConfigMap's
`BG_DEV_BOX_SSH_GRANT_ROLE_ARN` hold the same role ARN (then `adp devbox ssh <box>`
reaches the guest, #8049); the gateway's
`GET /cloud-desktops/policy` answers `admission_allowed: true` for a member of the
policy's organization; the UI shows Settings → Runtime → "Kiro Crew on my EC2 box"
and the chat "Open my EC2 Crew" action.

**Known limits (dev, 2026-10-17)**: the guest reaches only the gateway group and the
SSM endpoints (module contract; plus the S3 gateway endpoint prefix list when
`crew_bootstrap = user-data`). Model-route calls from the box to API Gateway need
a pinned Crew profile (`crew_profile_revision` + `crew_model_id`) — from a Crew image,
or from the pinned bundle installed at first boot in user-data mode (#8055) — which
also arms the guest's `execute-api` grant and the dashboard ingress; until then
`verify-dev-box.py` reports `crew_ready: false` (exit 0; `--require-crew` turns it
into exit 3). The first Crew image release on dev (`release-crew-image.sh --apply`,
#8054) is an operator step; its CodeBuild bundle build has not run yet at the time of
writing, and the user-data path consumes the same bundle it produces.

**Crew bootstrap validation (user-data mode)**: `aws ssm get-parameter --name
/adp/<env>/dev-box/crew-bootstrap` → `user-data`; `aws ec2 describe-launch-template-versions
--launch-template-id $(aws ssm get-parameter --name /adp/<env>/dev-box/launch-template-id
--query Parameter.Value --output text) --versions $(aws ssm get-parameter --name
/adp/<env>/dev-box/launch-template-version --query Parameter.Value --output text)
--query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d`
shows object paths, digests and revisions only (AC-04); on a box, `cat
/run/adp/crew-bootstrap.json` records `"status": "ok"` and `/var/log/adp-crew-bootstrap.log`
the timings (AC-03); `GET /cloud-desktops/{id}` reports `"crew_bootstrap": "user-data"`.

### Dev Box owner-shell acceptance (#8559)

This is a **post-deployment** procedure for the Dev Box delivery owner, not evidence that
these clients already work on a live box. First stage the approved Codex-capable Crew bundle,
rebuild the base/Crew image with the owner-shell publisher, stage the release and Replace one
box. Record the image, bundle, gateway and controller revisions and the box generation in a
private acceptance record. Use the box owner's own account, an authorized usage-log reader
and a quiet test window so unrelated turns cannot be mistaken for the two CLI calls. Do not
run `adp login` inside the box, copy the model key, print the environment or enable shell
tracing. Store sanitized transcripts and usage evidence privately, not in public docs.

**Operator terminal (outside the box):** Sign in to the ADP CLI on the operator machine
using its documented login flow. `<box-name>` is the name of a box owned by that account;
verify the SSH host-key fingerprint before accepting it. The CLI uses Session Manager for
SSH; these commands do not require inbound public SSH access.

```sh
adp devbox ssh-config <box-name>
adp devbox ssh <box-name>
```

**Owner's SSH/SSM terminal (inside the box):** Confirm this is a fresh `ec2-user` login
shell. Record the printed UTC time and run ID for the authorized usage query; neither is
a credential. The CLIs should answer `pong` within 30 seconds each, without a login or
permissions prompt. A failed call, a different answer, or a missing run ID fails the check.

```sh
whoami
type claude codex
date -u +'%Y-%m-%dT%H:%M:%SZ'
claude -p 'Reply with the single word pong.'
codex exec --skip-git-repo-check 'Reply with the single word pong.'
printf 'Run ID: %s\n' "$ADP_CREW_RUN_ID"
claude -p 'Call the adp-tools code_interpreter tool to evaluate 6*7. Reply only with the result.'
```

The final call should return `42` through `adp-tools` without a permission prompt; retain
the corresponding tools receipt privately. The tools check is separate from the two model
answers. The shell wrapper gets its per-boot model credential from an owner-only `/run`
file. Never display or archive that file, and do not mistake a successful local CLI exit
for proof of correct billing.

**Authorized usage-reader terminal (outside the box):** With read-only access to the
owner's usage rows, run the following *parameterized* query using the approved internal DB
client. Bind `:org_id` and `:owner_user_id` from the authenticated owner record,
`:run_id` from the run ID printed by the owner shell, and `:started_at` from the UTC time
printed before the calls. These are bind parameters, not literal values to paste into
public documentation; do not print connection strings or credentials.

```sql
SELECT timestamp, request_id, root_human_id, agent_run_id, model,
       input_tokens, output_tokens, cost_usd, status_code
FROM usage_logs
WHERE org_id = :org_id
  AND agent_run_id = :run_id
  AND timestamp >= :started_at
ORDER BY timestamp;
```

Find distinct successful rows for **both** the Claude and Codex calls (`status_code` 200,
nonzero tokens) in that time window. Every matched row must have `root_human_id` exactly
equal to `:owner_user_id` and `agent_run_id` exactly equal to `:run_id`, with cost charged
to that owner's budget. A row with a different owner, a null run/owner, or only a dashboard
turn is a failure even if a CLI printed `pong`; do not infer attribution from shell output.
If the authorized reader or live generation is unavailable, record these checks as
**not run**, not passed.

For the other live checks on this same generation, inspect `/run/adp-dev-box-shell`
permissions (`0700` directory, `0600` `model.env`, owned by `ec2-user`) without printing
the credential; confirm the latest checkpoint manifest has no shell credentials or Codex
rollout payload and its archive is under 512 MB. With authorized stop/start access, start
a slow model call over SSH, stop the box through the owner's
`POST /cloud-desktops/{desktop_id}/stop` API, and confirm the call fails (503 retry or
400 denied, never a sign-in prompt); correlate the private `adp-crew.service` journal's
`crew_model_adapter_failure` line. After starting a new generation, reconnect and confirm
a fresh call succeeds under its **new** run ID. These live actions require the delivery
owner's permissions and evidence; do not perform a stop solely to run local tests.

## Operator-set configuration a new environment must not miss

Everything below is declared in code and applied by the self-managed path
(`platform/scripts/deploy-all.sh`, including `--update`); this list exists because
each item was once set by hand on dev and would silently be absent in a fresh
environment. No GitHub Actions environment is required for any of it. The
GitHub-environment variables (`ADP_DEPLOY_ROLE_ARN`, `GATEWAY_HTTPS_ORIGIN`,
`ADP_CHAT_MODEL_POLICY_ENABLED`) only matter for the ADP-managed workflow path and
are documented with those workflows; the self-managed path derives the last two
from `--chat delegated` (see the chat rows below).

### Chief-of-Staff surfaces (#7197)

| What | Where it is declared | Applied / rendered by |
|---|---|---|
| `feature-chat`, `feature-assistant-ui`, `feature-crew-dashboard`, `crew-proxy-enabled`, `chat-authority-worker-images` SSM parameters | `modules/gateway/infra/chief-of-staff-flags.tf`, values in `environments/<env>/modules/gateway.tfvars` | deploy-all.sh Step 3 (adopts pre-existing parameters, then applies); rendered in Step 4 into `FEATURE_CHAT_ENABLED`, `FEATURE_ASSISTANT_UI_ENABLED`, `FEATURE_CREW_DASHBOARD`, `CREW_PROXY_ENABLED` |
| `crew-dashboard-origin` and the Crew public edge | `modules/gateway/infra/crew-dashboard.tf` (ALB mode) / `crew-dashboard-cloudfront.tf` (`crew_dashboard_edge = "cloudfront"`) behind `crew_dashboard_enabled` (hostname and zone in a private tfvars overlay) | same step into `CREW_DASHBOARD_ORIGIN` |
| Crew proxy tuning (#7902): SSM `/adp/<env>/gateway/crew-proxy-keepalive-seconds` (default 25; `0` disables) and `crew-proxy-max-conn-per-user` (default 64) | `modules/gateway/infra/chief-of-staff-flags.tf` (`crew_proxy_keepalive_seconds`, `crew_proxy_max_conn_per_user` in tfvars) | Step 4 and `gateway-deploy.yml` render `CREW_PROXY_KEEPALIVE_SECONDS` / `CREW_PROXY_MAX_CONN_PER_USER` into the ConfigMap and `UVICORN_WS_PING_INTERVAL` (browser-hop WebSocket ping) into the Deployment from the same keepalive parameter; at `0` the `UVICORN_WS_PING_INTERVAL` entry is deleted from the manifest rather than rendered as `0` (uvicorn treats 0 as a tight loop). Keep the keepalive below 60 in CloudFront edge mode. Absent or malformed parameters fall back to the defaults |
| Crew checkpoints (#7902): bucket `adp-<env>-crew-checkpoints-<account>`, IRSA grant, SSM `/adp/<env>/gateway/crew-checkpoints-enabled`, `crew-checkpoint-retention-seconds` (days x 86400) and, while on, `crew-checkpoint-bucket` | `modules/gateway/infra/crew-checkpoints.tf` behind `crew_checkpoints_enabled` (commented operator switch in `environments/<env>/modules/gateway.tfvars`; `crew_checkpoint_retention_days` default 30) | Step 3 creates them when the switch is on; Step 4 and `gateway-deploy.yml` render `ADP_CREW_CHECKPOINTS_ENABLED`, `ADP_CREW_CHECKPOINT_BUCKET` (absent => `""`, routes answer 503) and `ADP_CREW_CHECKPOINT_RETENTION_SECONDS` into the ConfigMap on every deploy |
| Gateway chat queue access: IAM policies `adp-<env>-policy-gateway-chat-{pending,response,completion}` and SSM `/adp/<env>/agent-gateway/{chat-input-queue-url,response-queue-url,sessions-table,context-table,ingest-function}` | `modules/agent-factory/infra/gateway-chat-*-access.tf`, `gateway-intake-access.tf` | deploy-all.sh Step 10 applies the stack; Step 4 renders the SSM values into `ADP_CHAT_*` / `BG_INTAKE_*` (ConfigMap parity test `platform/scripts/tests/test-configmap-render-parity.sh`) |
| Chat supervisor/worker roles, sandbox service accounts | `modules/agent-factory/infra` (`chat-*.tf`) | deploy-all.sh Step 10 (full apply) |
| Shared AgentCore user memory (#7901, code #7210): memory `adp_<env>_gateway_user_memory`, its strategy (`/adp/owners/{actorId}`), execution role, gateway IAM grant, SSM `/adp/<env>/gateway/agentcore-memory-{id,strategy-id,region}` and SecureString `/adp/<env>/gateway/agentcore-namespace-key` | `modules/gateway/infra/agentcore-memory.tf` behind `agentcore_memory_enabled` (commented operator switch in `environments/<env>/modules/gateway.tfvars`; default off) | deploy-all.sh Step 3 creates them when the switch is on; Step 4 and `gateway-deploy.yml` render `ADP_AGENTCORE_MEMORY_ID`, `ADP_AGENTCORE_MEMORY_STRATEGY_ID`, `ADP_AGENTCORE_MEMORY_REGION` into the ConfigMap and `ADP_AGENTCORE_NAMESPACE_KEY` (read with `--with-decryption`, never echoed) into the `bedrockgateway-secrets` Secret (`secretKeyRef`, `optional: true`) on every deploy. Switch off => all four empty => memory disabled; both renderers **fail the deploy** on a partial set (the gateway would raise `memory target unconfigured`) or a memory region other than the gateway's `AWS_REGION`. Wiring details and guards: `modules/gateway/docs/agentcore-memory-operator-handoff.md` |
| Chat supervisor + sandbox ScaledJob (delegated chat, Kiro Crew in the sandbox image) | `modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh` (full mode and `ADP_CHAT_CREW_CONTROLS_ONLY=true`), `platform/scripts/upgrade-chat.py --delegated` | deploy-all.sh Step 10 with **`--chat delegated --chat-exception <#6937 record>`** (#7899). The default `--chat retire` keeps today's behaviour (legacy worker drained, chat unavailable); `--chat keep-legacy` leaves the legacy credentialed worker running and skips retirement. In `delegated` mode Step 10 applies the factory with `chat_model_policy_enabled=true`, Step 10c builds `adp-chat-agent:<sha>` + `adp-chat-agent:sandbox-<sha>`, derives `GATEWAY_HTTPS_ORIGIN` from `/adp/<env>/gateway/apigw-invoke-url` (the gateway API Gateway stage URL `https://<id>.execute-api.<region>.amazonaws.com/<stage>`; fallback gateway infra output `api_gateway_invoke_url`) — the supervisor SigV4-signs its `/internal/v1/agent/chat/*` calls for `execute-api`, and the CloudFront frontend origin does not serve `/internal/*` — reads `/adp/<env>/gateway/crew-dashboard-origin` and `/adp/<env>/gateway/crew-s3-interface-ip` (both optional; `deploy-chat-scaledjob.sh` resolves the same two parameters itself whenever a caller leaves them unset, so `chat-agent-deploy.yml` converges to the same ScaledJob env, #8058), provisions the TLS endpoint (row below), applies the Crew controls and the ScaledJob — the script first pins both release digests into the `chat-agent-image-prepull` DaemonSet (containers `prepull` + `prepull-sandbox`) and waits for every node, so no first turn is killed by a multi-minute sandbox image pull (#8059); the ScaledJob also carries an explicit `ADP_CHAT_SANDBOX_START_DEADLINE_MS` (default 120000) — waits for it to be Ready, and only then runs `upgrade-chat.py drain/verify --delegated`. `chat-agent-deploy.yml` additionally runs `modules/agent-factory/agent/k8s/verify-chat-rollout.sh`, which fails the run when the live ScaledJob env or the DaemonSet digests drift from SSM / the build. The mode and record are written to the checkpoint journal (`notes.chat-mode`, `notes.chat-exception`). |
| Chat workload identities the gateway verifies pods against (`src/agentauth/workload.py`): SSM `/adp/<env>/gateway/chat-authority-worker-images` (supervisor digest), `chat-sandbox-image-digests` (sandbox pods, SA `adp-chat-sandbox`), `chat-crew-image-digests` (Crew pods, SA `adp-crew-pod`; the Crew pod template runs the sandbox image), `chat-worker-service-account` (`adp-agent` legacy / `adp-chat-supervisor` delegated) | Seeded by `modules/gateway/infra/chief-of-staff-flags.tf` (empty digests, `adp-agent`) with `lifecycle ignore_changes` on the value; **the deploy publishes release digests** | `modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh` publishes the three digest parameters (and confirms `chat-worker-service-account` / `chat-data-enabled`) right after the ScaledJob apply, then patches the live `bedrockgateway-config` (`ADP_CHAT_WORKER_IMAGE_DIGESTS`, `ADP_CHAT_WORKER_SERVICE_ACCOUNT`, `ADP_CHAT_SANDBOX_IMAGE_DIGESTS`, `ADP_CHAT_CREW_IMAGE_DIGESTS`, CA ConfigMap, data URL) with a gateway rollout restart (`ADP_CHAT_GATEWAY_ROLLOUT_TIMEOUT`, default 600s) — on `deploy-all.sh --chat delegated` (Step 10c) and `chat-agent-deploy.yml` alike (#8086; the ADP-managed identity `<prefix>-chat-trusted-deployment` holds `ssm:PutParameter` on the three parameters and the `adp:chat-release-publish` RBAC group from `platform/automation-infra/chat-release-publish-rbac.yaml`); `verify-chat-rollout.sh` fails the workflow when SSM, the ScaledJob and the ConfigMap digests disagree; Step 3 adopts pre-existing parameters; Step 4 and `gateway-deploy.yml` render them on every run (ConfigMap parity test) |
| Sandbox gateway TLS endpoint: Secret `chat-sandbox-gateway` + Deployment/Service/NetworkPolicy in `adp-gateway`, immutable CA ConfigMap `chat-sandbox-gateway-ca-<16hex>` + read Role/RoleBinding in `adp-gateway-agents` | `platform/scripts/render-chat-tls.py` (renders), `platform/scripts/apply-chat-tls.sh` + `chat-tls-issuer.py` (applies). Issuer material: Secrets Manager `adp/<env>/chat-sandbox-gateway/issuer` (JSON `ca_cert`, `ca_key`, `leaf_cert`, `leaf_key`). If absent, a **dev-only self-signed issuer is generated and stored there**, described as operator-owned and NOT for production; supply a reviewed issuer in that secret for anything else (`modules/agent-factory/infra/chat-sandbox-tls.md`). | deploy-all.sh Step 10c (`--chat delegated`) runs `apply-chat-tls.sh --env <env> --region <region>`; it is idempotent and also publishes SSM `/adp/<env>/gateway/chat-sandbox-ca-configmap`. **This SSM parameter and the issuer secret are owned by the script, not Terraform** (the value changes with the CA PEM). Both gateway renderers (deploy-all.sh Step 4 and `gateway-deploy.yml`) read the parameter into `ADP_CHAT_SANDBOX_CA_CONFIGMAP` and set `ADP_CHAT_DATA_URL=https://chat-sandbox-gateway.adp-gateway.svc:8443` when it is non-empty; Step 10c patches the live `bedrockgateway-config` in the same run. |

| Model-root bindings: SSM `/adp/<env>/gateway/model-root-bindings` (producer role, chat supervisor role, tenant, personas) | `modules/gateway/infra/chief-of-staff-flags.tf` (`model_root_bindings`), values in `environments/<env>/modules/gateway.tfvars` | deploy-all.sh Step 3 (adopts, applies); Step 4 and `gateway-deploy.yml` render into `ADP_MODEL_ROOT_BINDINGS`. Empty refuses every chat turn once model policy is on. |
| `AGENT_AUTHORITY_TABLE` | webhook-ingress SecureString `/adp/<env>/webhook-ingress/agent-authority-table` | both renderers fall back to `adp-<env>-agent-authority` when the renderer identity cannot decrypt it; an empty value disables model-root registration |

| Chat data plane switch `ADP_CHAT_DATA_ENABLED`: SSM `/adp/<env>/gateway/chat-data-enabled` | `modules/gateway/infra/chief-of-staff-flags.tf` seeds `false`; `deploy-all.sh --chat delegated` publishes `true` | both renderers; off ⇒ supervisors get 503 `chat_data_disabled` and no turn is answered |
| Chat memory and artifact stores (#8028): ConfigMap `MEMORY_TABLE` from SSM `/adp/<env>/gateway/chat-memory-table` (default `adp-<env>-agent-memory`), `ARTIFACTS_TABLE` from `chat-artifacts-table` (default `adp-<env>-chat-artifacts`), `ARTIFACTS_BUCKET` from `task-artifact-bucket-name` (default empty; the same read as `TASK_ARTIFACT_BUCKET_NAME`) | Tables, bucket and the two table parameters: `modules/agent-factory/infra/chat-agent-infra.tf`; the gateway role's bounded grants (`ChatMemoryStore`, `ChatArtifactCatalog`, `ChatArtifactObjects`, same KMS key as the intake tables): `modules/agent-factory/infra/gateway-intake-access.tf`; bucket parameter: gateway stack `task-api.tf` | deploy-all.sh Step 10 applies the parameters and grants (targeted: `aws_iam_policy.gateway_intake_access`, `aws_ssm_parameter.gateway_chat_memory_table`, `aws_ssm_parameter.gateway_chat_artifacts_table`); Step 4 and `gateway-deploy.yml` validate the three as names and render them on every deploy (absent or malformed ⇒ the table defaults, an empty bucket). **When a store is absent** the data plane answers 503 `chat_memory_unconfigured` / `chat_artifacts_unconfigured`; the sandbox logs one `[sandbox] memory|artifact store unconfigured on this gateway; continuing without ...` line, withdraws that store's tools, and still answers the turn (degraded, not failed). Any other data-plane 503 still fails the turn |

### Model policy posture for chat (#7997)

One admin-facing setting decides how strictly delegated chat applies the model
policy to a human's turn. It is `chat_model_policy_posture` in
`environments/<env>/modules/gateway.tfvars` (declared in
`modules/gateway/infra/chief-of-staff-flags.tf`), published as SSM
`/adp/<env>/gateway/chat-model-policy-posture` and rendered by both renderers
(`deploy-all.sh` Step 4, `gateway-deploy.yml`) into the gateway ConfigMap as
`BG_CHAT_MODEL_POLICY_POSTURE`. An absent or unrecognised value renders the default.

| Value | What chat does | When to use it |
|---|---|---|
| `advisory` (**default**) | Every turn still gets a signed, journaled model-policy decision for the persona's resolved model. If the invocability evidence check is rejected or unverified (`probing_disabled`, `evidence_stale`, `not_invocable`, …) the turn is **still answered**; the decision carries `posture: "advisory"`, `posture_verified: false` and the `rejection_reason`, and the gateway logs `Chat model policy decision accepted without verified evidence`. | A platform chatting on its own account; any environment that does not run the paid daily probe. Owner decision 2026-10-17. |
| `enforced` | Today's fail-closed path: a turn runs only on a verified, enforcing decision backed by fresh Claude invocability evidence; otherwise it is refused with 503 and the reason (`probing_disabled`, …). | Customer-connected destinations or when chat spend/compatibility must be proven before every turn. Requires the probing section below to be armed and recording evidence. |
| `off` | The decision is produced and signed without consulting any invocability evidence (`posture: "off"`). | Troubleshooting only. |

In every posture a model outside the tenant or persona allowlist stays refused
(`not_permitted`), outages still end the turn visibly, and autonomous agent runs are
unaffected (they keep the registry's per-class runtime posture). Changing the value:
edit the tfvars, apply gateway infra (Step 3) and re-run a gateway deploy (Step 4 or
`gateway-deploy.yml`); no image rebuild is needed. The delegated chat plumbing switch
(`chat_model_policy_enabled=true` on the ingest Lambda, `ADP_CHAT_MODEL_POLICY_ENABLED=true`
on the ScaledJob) stays on in every posture: without it the ingest answers "Chat is
currently unavailable" and the sandbox admission policies refuse the pods.

### Model invocability probing (required when the chat posture is `enforced`)

Under `chat_model_policy_posture = "enforced"` the gateway's model-policy plane
**fails closed** for chat: every `POST /v1/chat/model/decision` is refused with 503
`probing_disabled` unless fresh invocability evidence exists for the persona's model.
Only the invocability probe writes that evidence. Under the default `advisory`
posture (and `off`) chat answers without it, and the evidence still feeds autonomous
agent runs and the admin model picker. Until #7967 the probe controls were committed
literals in `modules/gateway/k8s/configmap.yaml` (off, zero slots, zero budget) and
the probe CronJob shipped suspended, so no environment could ever answer an enforced
chat turn. The decision-chain mismatch that kept probing off after the 2026-10-13
window (#7596) was fixed by #7605; probing no longer needs to stay off.

| What | Where it is declared | Applied / rendered by |
|---|---|---|
| Gateway probe controls: SSM `/adp/<env>/gateway/model-probe-enabled`, `model-probe-max-slots-per-cycle` (0–100), `model-probe-budget-usd-per-cycle` (decimal string), `model-probe-max-budget-usd-per-attempt` (decimal string), `model-probe-model-allowlist` (JSON array; `[]` = whole catalogue, including the Claude models chat resolves to), `model-probe-evidence-ttl-hours` (1–720) | `modules/gateway/infra/model-probe.tf` (`model_probe_enabled`, `model_probe_max_slots_per_cycle`, `model_probe_budget_usd_per_cycle`, `model_probe_max_budget_usd_per_attempt`, `model_probe_model_allowlist`, `model_probe_evidence_ttl_hours`), values in `environments/<env>/modules/gateway.tfvars`; defaults keep probing off | deploy-all.sh Step 3 applies the parameters; Step 4 and `gateway-deploy.yml` read, validate (bool / integer / decimal / JSON array of strings / integer; malformed => the fail-closed default) and render them into `BG_MODEL_PROBE_ENABLED`, `BG_MODEL_PROBE_MAX_SLOTS_PER_CYCLE`, `BG_MODEL_PROBE_BUDGET_USD_PER_CYCLE`, `BG_MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT`, `BG_MODEL_PROBE_MODEL_ALLOWLIST`, `BG_MODEL_PROBE_EVIDENCE_TTL_HOURS` on every deploy. Retained across release upgrades (`release_configuration`) |
| Probe CronJob `persona-model-invocability-probe` in `adp-agents` (daily `17 2 * * *`, suspended while off) | `modules/agent-factory/webhook-ingress/infra/persona-model-probe.tf` behind `persona_model_probe_enabled` in `environments/<env>/modules/webhook-ingress.tfvars` | deploy-all.sh Step 9 (`deploy-webhook-ingress.sh`); targeted: `terraform apply -target=kubernetes_cron_job_v1.persona_model_probe` |
| First probe cycle on demand | `platform/scripts/run-probe-cycle.sh` (env `ENVIRONMENT`, `NAMESPACE`=`adp-agents`, `CRONJOB`=`persona-model-invocability-probe`, `TIMEOUT_SECONDS`=900) creates a Job from the CronJob's own template, waits for Complete/Failed, prints the per-slot `[invocability-probe]` lines and the cycle summary (`completed`, `proven`/`refused`/`error`, `stoppedReason`) | deploy-all.sh Step 10c (`--chat delegated`) runs it right after the ScaledJob is Ready and before `upgrade-chat.py drain`; exit 0 = proven evidence recorded, exit 3 = nothing to claim (every catalogue model already fresh, or today's cycle spent; the deploy warns and continues), any other exit stops the rollout |

**Spend bound.** Dev runs `model_probe_enabled = true`, 24 slots, `"5"` USD per daily
cycle and `"0.50"` USD per attempt (the bound the owner approved in #7591), the whole
catalogue, and a 48 h evidence TTL so one missed daily cycle does not take chat down.
The gateway reserves the worst-case per-attempt spend before releasing credentials, so
a cycle can never exceed `model_probe_budget_usd_per_cycle`.

**Fail-closed behaviour.** `deploy-all.sh --chat delegated` reads the chat posture
first. Under `enforced` it refuses to proceed (before building any image) when
`/adp/<env>/gateway/model-probe-enabled` is not `true` or any of slots / cycle budget /
attempt budget is zero or unset, naming the tfvars to fix, and a failed probe cycle
stops the rollout before the legacy worker is drained. Under `advisory`/`off` the same
two checks only warn and the rollout continues.
`run-probe-cycle.sh` refuses (exit 4) when the CronJob is suspended and its own
`ADP_PERSONA_MODEL_PROBE_ENABLED` is not `true`, and exits 1 when the gateway refuses the
cycle (`disabled`, `zero_slots`, `zero_budget`, `platform_account_unconfigured`), the
Job fails, or every claimed slot ended `refused`/`error`.

**Request-shape manifest and the CronJob image pin.** A Claude probe only forwards a
request whose normalized body digest equals the gateway's expected digest for that
model; otherwise it ends `request_shape_mismatch`, writes no evidence and the slot's
budget is spent for nothing. Both digests come from one checked-in manifest
(`modules/agent-factory/agent/src/invocability-probe/request-shape-manifest.json` and
its byte-identical copy `modules/gateway/src/admin/persona_models/request-shape-manifest.json`),
so three things must come from the same commit: the gateway image (manifest), the
`agent_image` digest pinned for the CronJob in `environments/<env>/modules/webhook-ingress.tfvars`
(manifest plus the `canonical-json.ts` normalization that produces the digests), and the
manifest itself, which must be **generated from that runtime image**, never from a
developer machine (#7993: the 2026-09-27 manifest carried a developer host's shell,
kernel and date and never matched a pod). Procedure and the exact `kubectl create job`
command: `modules/agent-factory/agent/src/invocability-probe/task-probes.md`. Order
after a normalization or SDK change: build the agent-runtime image from the merge
commit, run the generator Job from that image, commit both manifest copies (the
`Claude Request-Shape Manifest Drift` CI job reproduces them from the pinned SDK on a
plain runner and fails on drift), bump `agent_image`, targeted CronJob apply,
gateway-deploy, then `platform/scripts/run-probe-cycle.sh`. To see why a probe was
refused, run a one-off Job copied from the CronJob template with `ADP_PROBE_PRINT_BODY=1`;
the runner prints the canonical body of the refused request (stderr, chunked, off by
default).

**Recovery.** Chat answering 503 `probing_disabled` or `evidence_stale` (only possible
under `enforced`) means the evidence lapsed or was never recorded: check `kubectl -n adp-agents get cronjob
persona-model-invocability-probe` is not suspended, that the live ConfigMap carries
`BG_MODEL_PROBE_ENABLED: "true"` with non-zero slots and budgets, then run
`ENVIRONMENT=<env> platform/scripts/run-probe-cycle.sh`. To switch probing off again,
set `model_probe_enabled = false` (gateway tfvars) and `persona_model_probe_enabled =
false` (webhook-ingress tfvars) and redeploy; the CronJob re-suspends and the gateway
refuses every claim. Note that under `enforced` delegated chat then stops answering
once the evidence TTL lapses; under `advisory` it keeps answering and journals the
rejection reason.

### Dev Box (optional module, #7998)

| What | Where it is declared | Applied / rendered by |
|---|---|---|
| Stage selection: `--dev-box on\|off`, `ADP_DEV_BOX`, `--dev-box-only`; default off unless `stage_enabled = true` in the environment's tfvars | `environments/<env>/modules/dev-box.tfvars` (+ `dev-box-platform.tfvars`, both backend files); dev ships them | deploy-all.sh Step 12 → `modules/user-services/dev-box/scripts/deploy-dev-box.sh`; journal `notes.dev-box-mode` (written only when on; a default run's journal is unchanged); the mode is part of the resume binding |
| Platform prerequisites: KMS key `alias/adp-<env>-dev-box-workspace`, SSM interface endpoints + group, CodeBuild `adp-<env>-dev-box-image`; SSM `/adp/<env>/dev-box/{kms-key-arn,ssm-endpoint-security-group-id,image-builder-project}` | `modules/user-services/dev-box/infra/platform` | stage step 1 |
| AMI: SSM `/adp/<env>/dev-box/{image-id,image-revision,image-source-revision,base-image-id,profile-revision}` (script-owned) | `modules/user-services/dev-box/image/buildspec.yml`, `image/*.pkr.hcl` | `scripts/build-dev-box-image.sh` (stage step 2 when image-id is absent, or on demand with the Crew bundle) |
| Foundation: guest/controller IAM, launch template, controller runtime IRSA role `adp-<env>-dev-box-controller-runtime`; SSM `/adp/<env>/dev-box/{deployment-id,max-deployment-*,policy-org-id,launch-template-*,controller-*,guest-*,run-table-arn,gateway-execute-arn,ssh-grant-role-arn,...}` | `modules/user-services/dev-box/infra` (`main.tf` + `self_managed.tf` + `ssh-grant.tf`), values in `dev-box.tfvars`; `image_id`/`source_revision`/`crew_profile_revision`/`kms_key_arn`/`ssm_endpoint_security_group_id`/`eks_oidc_provider_arn`/`rds_resource_id`/`gateway_principal_arn` resolved by the stage | stage step 3 |
| SSH grant (#8049): role `adp-dev-box-ssh-grant-*` trusting only the gateway IRSA role, inline policy `adp-dev-box-assume-ssh-grant` on that gateway role, SSM `ssh-grant-role-arn` | `modules/user-services/dev-box/infra/ssh-grant.tf`; `gateway_principal_arn` read by the stage from `serviceaccount/gateway-service` (`dev-box-deploy.yml`: `ADP_DEV_BOX_GATEWAY_PRINCIPAL_ARN`) | stage step 3; without it `adp devbox ssh` gets 503 "Dev Box SSH access is not configured" |
| Gateway keys `BG_DEV_BOX_DEPLOYMENT_ID`, `BG_DEV_BOX_MAX_DEPLOYMENT_{LIVE_BOXES,RUNNING_BOXES,STORAGE_GB,RUNNING_HOURS,BUDGET_USD}`, `BG_DEV_BOX_SSH_GRANT_ROLE_ARN`, `BG_DEV_BOX_CREW_BOOTSTRAP` | ConfigMap placeholders in `modules/gateway/k8s/configmap.yaml`; parity test `modules/gateway/tests/features/test_dev_box_flag_parity.py` | Step 4 and `gateway-deploy.yml` read `/adp/<env>/dev-box/*` on every deploy (absent ⇒ `""` / `"0"` / `image`, admission disabled, SSH access 503); stage step 5 patches the live ConfigMap in the same run |
| Crew bootstrap mode (#8055): `crew_bootstrap = image` (default; profile baked into the AMI, no user data) or `user-data` (base AMI installs the pinned bundle at first boot); SSM `/adp/<env>/dev-box/crew-bootstrap` (the digest and revision come from the bundle build's script-owned `crew-bundle-sha256` / `crew-bundle-revision`, #8054); guest grant `crew-bootstrap-inputs-read` + S3 gateway-endpoint egress in user-data mode only | `modules/user-services/dev-box/infra/crew_bootstrap.tf`, `infra/crew-bootstrap.sh.tftpl`; stage flags `--crew-bootstrap --crew-bundle-sha --profile-revision --crew-model-id` (mode persists in SSM for flag-less re-runs); workflow `ADP_DEV_BOX_CREW_*` variables | stage step 3 (new launch template version); the controller fails closed without the on-box receipt `/run/adp/crew-bootstrap.json` |
| Controller Deployment `dev-box-controller` (namespace `adp-gateway`, SA `dev-box-controller`, gateway image, ConfigMaps `dev-box-controller-code` + `dev-box-release`) | `modules/user-services/dev-box/k8s/controller.yaml` | stage step 5 |
| Admission rows (`dev_box_policies`, `dev_box_template_versions`) and the organization's `crew.allowed_targets` += `ec2-instance` | Terraform output `policy_spec` → `python -m src.cloud_desktop.seed` (`modules/gateway/src/cloud_desktop/seed.py`) | stage step 6, via `kubectl exec` into the gateway; never raw SQL |
| Live acceptance (AC-01, #7223 re-run) | `modules/user-services/dev-box/scripts/verify-dev-box.py` | stage step 7 when a token is available; otherwise run it as the operator |

### Orchestration runtime verifier access

| What | Where it is declared | Applied by |
|---|---|---|
| EKS access entry mapping the connection role (e.g. `ADP-Agent-<env>-platform`) to group `adp:runtime-verifier-<env>` | `platform/infra/runtime-verifier-access.tf` (`runtime_verifier_principal_arn`, supplied as `TF_VAR_runtime_verifier_principal_arn` once the account is connected) | deploy-all.sh Step 2 |
| Role `adp-runtime-verifier` in `adp-gateway` | `modules/agent-factory/infra/runtime-verifier-rbac.tf` | deploy-all.sh Step 10 (adopts the live dev objects first) |
| Role `adp-runtime-verifier` in `agent-context` | `modules/agent-context/manifests/ingestion-rbac.yaml` | deploy-all.sh Step 11 via `modules/agent-context/deploy.sh` (with `ENVIRONMENT` exported) |
| Role `door-identity-keys-read` in `adp-agents` for group `adp:trusted-deployment` | `modules/agent-factory/webhook-ingress/infra/door-identity-rbac.tf` | deploy-all.sh Step 9 via `deploy-webhook-ingress.sh` (adopts the live dev objects first) |
| Agent worker memory (12Gi, #7769) | `environments/<env>/modules/webhook-ingress.tfvars` (`agent_worker_memory_*`) | deploy-all.sh Step 9 |

### Gateway rollout

deploy-all.sh Step 4 runs `modules/gateway/scripts/migrate-before-rollout.py` against the
release image before the Deployment changes, the same as `gateway-deploy.yml`; a failed
migration leaves the previous release serving.

### Gateway service role IAM budget

`adp-<env>-role-gateway-service` (created by `platform/infra/modules/eks/main.tf`) is
written to by four stacks, and IAM caps it at **10,240 characters of inline policy in
aggregate** and **20 attached managed policies** (the account quota is already at the
20 maximum). On dev the inline aggregate reached 10,144 bytes across 14 inline
policies and every gateway apply that touched one failed with
`LimitExceeded: Maximum policy size of 10240 bytes exceeded`.

The gateway module no longer puts inline policies on the role. Its grants are three
customer managed policies declared in `modules/gateway/infra/gateway-role-policies.tf`
(same statements, same Sids, same resources and conditions as the retired inline
policies):

| Policy | Carries | Size (dev / worst case) |
|---|---|---|
| `adp-<env>-policy-gateway-identity` | Cognito read + lifecycle (`CognitoReadUsers`, `CognitoIdentityLifecycle`, `CognitoMachineClientLifecycle`, `CognitoCliLoginMint`), identity-index and user-identity-index tables, user-scoped AgentCore OAuth, agent client credentials | 3,234 / 3,353 of 6,144 |
| `adp-<env>-policy-gateway-data` | Vault secrets + their CMK, CFN template bucket, chat-log / Comprehend / X-Ray sinks, RDS and ElastiCache IAM auth, ingestion queue | 2,895 / 3,168 of 6,144 |
| `adp-<env>-policy-gateway-agents` | Mantle / pool model invocation, tool catalogue (was `bedrockgw-<env>-gateway-tools-catalogue-read`), run transcripts, crew run bindings and checkpoints, AgentCore memory | 1,255 / 2,495 of 6,144 |

`modules/gateway/tests/infra/test_gateway_role_iam_budget.py` renders these documents
and the other stacks' inline grants from the `.tf` source and fails when a managed
policy passes 6,144 characters, the inline aggregate passes 8,192, or the attachments
every stack declares can exceed 20.

**Who uses the budget after the cutover (dev).** Inline, 3,449 of 10,240 characters:
`platform/infra` (`adp-<env>-policy-gateway-{bedrock,sts,irsa-logs,ssm-read,dynamodb}`)
and `webhook-ingress` (`adp-<env>-policy-gateway-{activity-read,agent-authority,agent-self-write,service-approval}`).
Attached, 19 declared + 1 live-only of 20: gateway 4 (`identity`, `data`, `agents`,
`task-api`), `platform/automation-infra` 1 (`deployment-evidence-read`), agent-factory 4
(`intake`, `chat-{pending,response,completion}`), webhook-ingress 3 (`authorized-dispatch`,
`task-source`, `task-storage` — declared, not yet attached on dev), Superplane 7
(`gateway-domain-operations`, `superplane-gateway-route-read`,
`superplane-provider-authority` and **one `adp-<env>-spp-<hash>-gateway-access` per
domain provider — four on dev**), plus the live-only `adp-dev-authority-probe-20260920`
(unknown ownership; do not delete). That is the quota with zero headroom: the next
attachment anywhere needs the Superplane per-provider policies consolidated into one
(saves three slots) or the Task API policy folded into `agents` (saves one).

**Upgrading an environment that still has the inline policies.** Two complete passes,
so the role never loses a permission mid-apply; `deploy-all.sh --update` Step 3 runs
them automatically and the manual sequence is:

```bash
ROLE=adp-dev-role-gateway-service
# 0. Pass 1 attaches three policies. The role holds 18 on dev, so free one slot first:
#    the hand-created adp-dev-tools-catalogue-read duplicates the gateway's
#    bedrockgw-dev-gateway-tools-catalogue-read. Confirm, then detach ONLY the duplicate.
aws iam get-policy-version --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/adp-dev-tools-catalogue-read \
  --version-id "$(aws iam get-policy --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/adp-dev-tools-catalogue-read --query Policy.DefaultVersionId --output text)" \
  --query PolicyVersion.Document
aws iam detach-role-policy --role-name $ROLE --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/adp-dev-tools-catalogue-read

# 1. Capture the live inline documents and decide the phase (reads only; exits 2 with the
#    attachment list if the quota still cannot take pass 1).
python3 modules/gateway/scripts/gateway-role-policy-cutover.py --environment dev --region us-east-1 \
  --output /tmp/gateway-role-cutover.tfvars.json        # prints: cutover | retire | complete

# 2. Pass 1 (only when it printed "cutover"): create + attach the managed policies, keep
#    every live inline policy byte-for-byte (no PutRolePolicy at the limit).
cd modules/gateway/infra
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars \
  -var-file=/tmp/gateway-role-cutover.tfvars.json -auto-approve
aws iam list-attached-role-policies --role-name $ROLE --query 'AttachedPolicies[?starts_with(PolicyName,`adp-dev-policy-gateway-`)].PolicyName'

# 3. Pass 2: defaults. Destroys the retained bedrockgw-dev-* inline policies, the retired
#    bedrockgw-dev-gateway-tools-catalogue-read attachment/policy and the never-attached
#    bedrockgw-dev-policy-gateway-cognito-lifecycle. With deploy-all.sh --update these
#    destroys are routine only when the attached replacement contains every retired
#    statement (platform/scripts/upgrade-plan-policy.py, gateway_role_policy_consolidation).
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars -auto-approve

# 4. Verify: no bedrockgw-dev-* inline policy remains and the aggregate is small.
aws iam list-role-policies --role-name $ROLE
```

Pass 1 maps each live inline policy onto `aws_iam_role_policy.gateway_role_legacy["<suffix>"]`
through `moved` blocks (keyed by the suffix of its `bedrockgw-<env>-[policy-]gateway-<suffix>`
name), so nothing is imported by hand and pass 2 is an ordinary destroy. A fresh
environment never sets the cutover variables and gets only the three managed policies.
If pass 2 is interrupted, re-running it is safe: the managed policies are already attached
and the gate re-evaluates each remaining destroy.

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

Installation-owned evaluator smoke fixtures have a separate state owner. Use
`platform/scripts/undeploy.sh --evaluator-fixtures --help` to prepare a private
delete-only plan for the exact evaluator target, review it, and repeat with
`--apply`. This checks fixture IAM identities, security-group ownership and no
attached network interfaces before deletion; shared evaluator evidence and
credentials retain their separate cleanup policy.

### Primary: `undeploy.sh` (self-managed)

```bash
# Self-managed — interactive, typed-account-ID gate
./platform/scripts/undeploy.sh

# Dry-run first (recommended) — shows what exists, what would be destroyed
./platform/scripts/undeploy.sh --dry-run

# Include state backend destruction
./platform/scripts/undeploy.sh --bootstrap
```

The `undeploy.yml` workflow is not a supported customer cross-account path.
Supplying any legacy customer-account input fails in the shared config action
before a destroy step. `undeploy.sh` destroys in reverse dependency order:
superplane → agent-context → agent-factory → webhook-ingress → gateway → platform.

The factory must be removed while webhook-owned KEDA is still available.
After all plans pass, schedules and the gateway application are stopped before
removing downstream consumers; gateway infrastructure remains available for
Terraform lookups. KEDA jobs and authentication resources drain before its operator
is uninstalled, without forced finalizer removal. Within
modules, teardown removes Lambda functions and waits for their network interfaces
before deleting execution roles; it removes Kubernetes resources before Helm,
cluster access, or EKS. Running CodeBuild jobs and log delivery stop before their
stores are emptied. Any error stops the run and preserves later dependencies.

`--dry-run` initializes Terraform and validates saved delete-only plans for every
selected module. It makes local private files but changes no AWS or Kubernetes
resources. It uses recorded release inputs where available; older modules may
require their original target-specific tfvars. Missing local Lambda code is
recovered from the deployed artifact, with its checksum verified.

Plans, state backups, retained identifiers, and the journal are stored under
`.adp-teardown/<account>-<region>-<environment>/` (gitignored, private permissions).
Set `ADP_TEARDOWN_DIR` to use another private directory. Keep this evidence for
recovery; do not upload it to public issues. Re-run the same command after resolving
a failure: live state is re-read and plans are rebuilt, rather than trusting a
previous "complete" flag. A stale `running.lock` may be removed only after checking
that the recorded process has exited.

Use `--retain-vpc` when independent resources occupy the ADP VPC. The VPC and default
security group remain in Terraform state; the script does not delete unfamiliar
security groups. EKS Auto Mode load-balancer groups are recognized by their cluster
and managed-resource tags. CloudFront's shared VPC-origin group is accepted only
when every live origin in that VPC belongs to the selected Terraform state; the
gateway's CloudFront distribution and VPC origin are removed before its ingress.
`--skip` and `--from` are refused if an omitted deployed consumer
still requires a selected dependency. `--bootstrap` refuses to delete a backend
that still tracks resources, including intentionally retained credentials.

Before removing the Superplane control plane, webhook, gateway or platform,
the script also discovers separately managed Superplane workspace, reader,
runtime, provider and foundation states in the selected environment. Nonempty
states stop core teardown before any deletion and are recorded in the private
`superplane-auxiliary-inventory.json`. Remove those resources through their
app-owned lifecycle scripts in dependency order, then repeat the preview. This
check also refuses unfamiliar Superplane state owners; it never discards state
or treats core module deletion as a complete workspace reset.
The launcher exposes app-owned preparation, quiesce and apply commands; follow
[Superplane decommissioning](../../modules/domain-apps/superplane/scripts/TEARDOWN.md)
for their scope and dependency order.

Teardown empties only buckets and repositories in its reviewed deletion plans.
It also removes service-created log groups tied to recorded Lambda, CodeBuild,
EKS and API Gateway resources, plus the three application signing secrets created
by the gateway installer. Runtime cleanup records log creation times and secret
ARNs so a replaced resource is refused on retry. User vault and GitHub/OAuth
credentials are not swept.
It does not sweep an account by name prefix. Successful module deletion is distinct
from an account-wide cleanup audit; independent resources and AWS pending deletion
periods must be reported separately.

`undeploy.sh --aws-profile customer` selects credentials using the same option
and precedence as `deploy.sh`. The `AWS_PROFILE` environment variable remains
supported when the flag is omitted.

For an immediate teardown/reinstall, add `--purge-deleted-secrets` to both the
dry run and the approved teardown command. This permanently purges secrets
selected by the Terraform deletion plans after Terraform schedules deletion,
then waits for AWS to report them absent. It excludes retained GitHub credentials
and does not sweep untracked secrets. Without this option, recovery windows stay
in effect and secret names can block reinstall for up to 30 days. Either wait,
restore and import the exact secret after checking its encryption key, or rerun
teardown with this option using the retained private deletion receipts. A purge
timeout stops teardown; retry before reinstalling. KMS deletion waiting periods
are unchanged.

### Legacy path (retained)

```bash
./platform/scripts/deploy-all.sh --destroy      # LEGACY — lacks account-ID gate, dry-run, resume
./platform/scripts/bootstrap-destroy.sh         # state backend, separate, typed confirmation
```

> `deploy-all.sh --destroy` is retained for backward compatibility but is no
> longer recommended. Use `undeploy.sh` instead.

### Resources that survive by design

- Terraform state backend (until `--bootstrap` / `include_bootstrap`)
- GitHub App secrets (legacy and environment-scoped), webhook HMAC secret, and their encryption key
- Account-wide ECR registry scanning configuration
- GitHub Apps themselves (delete manually in org settings)
- AWS-managed RDS secrets (`rds!*`)

### Verify gateway and scheduled-engine image parity after updates

A gateway image has two runtime consumers: the EKS gateway and the scheduled
orchestration Lambda. Both the CI workflow and `deploy-all.sh` verify that they
use the same release digest. Manual or interrupted rollouts must also run the
[shared alignment and verification helper](../runbooks/shared-worker-flow-continuation.md#gateway--scheduled-engine-release-parity)
before reporting completion. Pushing an image to ECR or checking EKS readiness
alone does not verify the Lambda release.

### Optional menu entries

The gateway deployment reads SSM `/adp/<env>/gateway/feature-knowledge`,
`feature-indexing`, and `feature-tenant-org-links`. Set the corresponding value
to `false` and run the upgrade to hide Knowledge, Indexing Status, or the legacy
Tenant Org Links menu. Knowledge and indexing default to enabled; legacy links
default to hidden. These display flags do not delete stored data, remove teams,
or stop indexing workers.

### Gateway rollout budget

The platform deploy script preserves the gateway's current desired replica count
when applying an upgrade, including a count managed by the horizontal pod
autoscaler. If that count cannot be read, the upgrade stops before applying the
gateway Deployment. Fresh deployments start with two replicas.

Gateway rollout checks default to 2,400 seconds (40 minutes), allowing for node
provisioning, application startup, and the existing 960-second graceful shutdown
window for active model streams. Set `ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS` to a
positive integer in seconds to override this total wait limit, for example:

```bash
ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS=3000 ./deploy.sh --update
```

The timeout does not restart the rollout or extend itself indefinitely. On
failure, the script prints replica/pod status, Deployment conditions, and namespace
events before stopping. Treat these diagnostics as private operational evidence.
The script does not change node consolidation policy.

### Runner pools: CI shards vs deploys (2026-10-20)

Two Actions Runner Controller scale sets share one controller, namespace,
GitHub App secret and IRSA service account (`github-runner-sa`):

| Pool (`runs-on`) | Declared in | Who uses it | Size |
|---|---|---|---|
| `arc-runner-org` | `modules/agent-factory/infra/modules/arc-runner` | CI shards, security scans, agent runs, everything not listed below | min 1 / max 60 (PR #8393; 40 before) |
| `arc-runner-deploy` | `modules/agent-factory/infra/modules/deploy-runner-pool` | deploy/apply workflows and the reusable workflows only they call (`gateway-deploy`, `*-deploy`, `_deploy-eks`, `platform-deploy-mgmt-infra-apply`, `run-gateway-migrations`, `gateway-smoke`, `tools-facade-deploy`) | min 1 / max 8 |

Why: on 2026-10-20 all 40 org runners were running Gateway CI shards and the
tail jobs of a gateway deploy (controller refresh, migrations, smoke) queued for
20+ minutes with no runner after the backend had already rolled out. A pool that
test shards cannot target cannot be starved by them. Moving a job between pools
changes nothing about its identity or grants.

The trusted `arc-runner-deployment` set described in
`platform/automation-infra/runner-values.yaml` (own GitHub App, runner group,
tainted `adp-trusted-deployment` NodePool, reviewed image digest) remains the
long-term design; the deploy pool exposes `node_selector`/`tolerations` so it
can move onto that NodePool when those pieces land.

Self-managed apply (dev has no GitHub-dispatched deployment role):

```bash
cd modules/agent-factory/infra
terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars -input=false
terraform plan  -target=module.deploy_runner_pool -var-file=terraform.tfvars -input=false -out=/tmp/deploy-pool.tfplan
terraform apply /tmp/deploy-pool.tfplan
kubectl -n arc-runners get autoscalingrunnersets.actions.github.com   # arc-runner-deploy MIN 1 MAX 8
```

Order matters: apply the pool **before** merging any `runs-on: arc-runner-deploy`
change. GitHub has no runner fallback, so a label with no registered listener
leaves every job queued forever. `.github/runner-pools.json` pins which workflows
belong to the deploy pool (read by the release, trust-contract and CI-evidence tests); a workflow that
also runs as a pull-request check must stay on `arc-runner-org`
(`adp-deploy-check.yml`, `gateway-live-tests.yml`, `eval-cli-uplift.yml` do).
