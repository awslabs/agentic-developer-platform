# Deploying ADP — What To Expect

This is a human-facing walkthrough of deploying ADP from a fresh clone. It describes what you'll see, what you're expected to do, and what could go wrong at each stage. Read this before you start — **there is one phase (GitHub Apps) that can't be automated and needs ~10 minutes of your attention.**

For the exact commands and machine-readable phase definitions, see [`AGENTS.md`](../../AGENTS.md) (the agent instructions) and [`deployment-manifest.md`](./deployment-manifest.md) (per-resource validation).

## Before You Start

You need:

- **An AWS account** you have admin access to. A dev account is fine — don't use prod for first-time deploys.
- **AWS CLI configured** with a profile that resolves to that account. Check with:
  ```
  aws sts get-caller-identity
  ```
- **A GitHub organization** you own, where the agents will live. (Not just a user account — the Agent Factory requires org-level GitHub App installation.)
- **`gh` (GitHub CLI) authenticated**. Check with `gh auth status`.
- **These CLI tools**: `terraform` (1.5+), `kubectl`, `node` (22+), `npm`, `docker` (only for local development; CodeBuild handles production image builds).
- **~45 minutes** of wall-clock time. Most of it is waiting for AWS (EKS takes 15 min to provision; CloudFront takes 5-10 to propagate).
- **~$5-10 per day of idle AWS cost** for the dev configuration (RDS Multi-AZ, NAT Gateway, CloudFront). Teardown when done if you're cost-conscious.

## The 5 Phases

### Phase 0 — GitHub Setup (~10 min, needs you)

**This is the only phase that can't be automated.** The agent will ask you five questions and then open your browser three times.

**What you'll be asked:**

1. **Your GitHub org name.** E.g. `aws-e`. The agents impersonate `<org>-adp-agent-dev`, `-pm`, `-ops` and need to live somewhere that's not your personal account.
2. **Repo name.** Default `adp`. Whatever you called your fork/clone of this repo inside your org.
3. **AWS profile.** If you have multiple profiles, pick the one whose account you want to deploy to. The agent runs `aws sts get-caller-identity` and shows the account ID for you to confirm.
4. **Which modules you want.** Three options:
   - *Everything* — Gateway (Bedrock proxy) + Agent Factory (code-writing agents) + their shared platform.
   - *Gateway only* — if you just want the Bedrock proxy. Cheaper and simpler. Skips the GitHub App setup entirely.
   - *Agent Factory only* — if you only want the autonomous code agents.
5. **Extra repos for the agents to work on.** Optional. The GitHub Apps can be installed on multiple repos in your org — list any others besides this one.

**Then the browser clicks.**

If you chose *Everything* or *Agent Factory only*, the agent needs to create three GitHub Apps — one for each of the dev, pm, and ops agent personas. This cannot be automated because GitHub only issues app credentials through the web UI.

For each of the three apps, you'll:

1. The agent opens your browser to `https://github.com/organizations/<your-org>/settings/apps/new` with a pre-filled form. **Click "Create GitHub App" at the bottom** — permissions are already set, don't change them.
2. GitHub shows you the app page. **Copy the App ID from the top** (e.g. `123456`). Paste it when the agent prompts you in your terminal.
3. Scroll down on the same page and click **"Generate a private key"**. A `.pem` file downloads to your `~/Downloads`. The agent auto-detects it — you don't need to move it.
4. The agent opens your browser again, this time to the app's install page. **Select your org, choose "Only select repositories", pick the repo(s) you want**, click **Install**.
5. Press Enter in the terminal to continue to the next app.

Repeat for the other two apps (pm and ops). All three apps are named `<your-org>-adp-agent-<role>` — GitHub App names are globally unique, so the org prefix keeps yours from colliding with someone else's.

The credentials land in AWS Secrets Manager at `adp/<org>/gh-app-<role>-id` and `adp/<org>/gh-app-<role>-key`. You don't need to do anything with them after this — Terraform reads them when it builds the ARC runner.

**Time:** ~10 minutes if you're attentive, more if you get distracted. You must stay at your terminal for this phase.

**If you're resuming:** The agent checks for existing apps first (`aws secretsmanager list-secrets`) and skips this step if all three are already configured.

---

### Phase 1 — Bootstrap Terraform State (~1 min, automated)

Creates two AWS resources **in the account your credentials resolve to**, that every subsequent step depends on:

- **S3 bucket** `adp-terraform-state-<your-account-id>` — stores the Terraform state for all four modules under keys like `dev/platform/terraform.tfstate`. Versioned, encrypted (AES256), public access blocked.
- **DynamoDB table** `adp-terraform-locks` — coordinates concurrent runs so two `terraform apply` operations can't clobber each other.

The script also rewrites `environments/dev/backend.tfvars` (and per-module backend files) in your local checkout to point at your account's bucket. **Don't commit that rewrite** — every operator/agent gets a fresh substitution at bootstrap time.

**What you'll see:** "Terraform state backend ready in account XXXX. S3 bucket: adp-terraform-state-XXXX." No interactive prompts.

**Idempotent** — safe to re-run if something failed later; the script no-ops if both resources already exist.

---

### Phase 2 — Preflight (~1 min, automated)

The agent runs `./platform/scripts/preflight-check.sh` to verify your environment **after bootstrap**:

- All required CLI tools are installed and work (aws, terraform, node ≥ 22 must succeed; docker + gh are warn-only).
- AWS credentials are valid and resolve to the account you confirmed.
- Your identity has the **minimum** IAM permissions needed for the next steps:
  - **Required (FAIL on missing):** s3:ListAllMyBuckets, dynamodb:ListTables, eks:ListClusters, ecr:DescribeRepositories.
  - **Recommended (WARN on missing — needed by Phases 3-8):** iam:Get*, codebuild:ListProjects, bedrock:ListFoundationModels, secretsmanager:ListSecrets, cognito-idp:ListUserPools.
- The Terraform state bucket and lock table from Phase 1 are reachable.
- Your `environments/dev/backend.tfvars` was substituted by bootstrap (no `ACCOUNT_ID` placeholder remains).

**What you'll see:** a list of checks with ✓ / ✗ / ⚠ markers, ending with `Results: N passed, M warnings, K failed`. The agent will read those numbers, report any warnings to you, and continue if `K = 0`.

**What's NOT explicitly checked but you should validate yourself if your role is scoped:** RDS, ElastiCache, CloudFront, CloudWatch Logs, Lambda, and API Gateway permissions. These are exercised by Phases 4–8. If you're using an admin-level role, no concern.

**What could fail:**
- Missing `terraform` / `node` / `aws` → the agent tells you exactly what to install and waits.
- Wrong AWS profile / expired creds → the agent stops and asks you to fix `aws configure` or `AWS_PROFILE`.
- Missing required IAM permissions → the agent lists what's missing. Typically means you're not admin on the target account.
- State bucket / lock table not reachable → Phase 1 didn't complete; re-run bootstrap before re-running preflight.

---

### Phase 3 — Full Deploy (~30 min, automated, the main event)

The agent runs `./platform/scripts/deploy-all.sh`. This is the one command that does everything from "empty AWS account" to "live service". It's broken into 11 internal steps, and the agent reports on each:

1. **Preflight re-check** (~10 sec). Same as Phase 1, in case anything drifted.
2. **Detect your public IP** and lock the EKS Kubernetes API to your `/32` so nobody else can `kubectl get`. Takes a second.
3. **Platform infrastructure** (~15 min). Creates the VPC, EKS cluster (Auto Mode), ECR repositories, IAM baseline, and the four shared CodeBuild projects used for Docker image builds. This is the longest single step — EKS takes a while to provision.
4. **Agent Factory infrastructure** (~5 min, skipped if you chose Gateway only). Creates the IAM role for your CI runner, deploys the Actions Runner Controller (ARC) + KEDA via Helm, creates SQS queues and DynamoDB tables for the agent delivery pipeline.
5. **Agent Gateway build + deploy** (~3 min, same skip). Packages the TypeScript chat-agent source, builds a Docker image in CodeBuild, pushes to ECR, applies the K8s manifests.
6. **Gateway infrastructure first pass** (~5 min, skipped if you chose Agent Factory only). Creates RDS PostgreSQL, ElastiCache Redis, Cognito user pool, CloudFront distribution, API Gateway, Lambda authorizer. On this first pass the API Gateway has placeholder (MOCK) integrations because the internal load balancer doesn't exist yet.
7. **Wait for EKS Ingress ALB** (~1-2 min). Polls until the Kubernetes Ingress controller provisions the internal ALB. Caches the details to SSM so you don't have to re-discover them later.
8. **Gateway infrastructure second pass** (~2 min). Same Terraform, now with the real ALB details, flipping the API Gateway from MOCK integrations to actual traffic routing.
9. **Gateway backend image** (~3 min). Builds the FastAPI server image in CodeBuild, pushes to ECR, rolls the Kubernetes deployment to pick up the new image.
10. **Gateway frontend** (~2 min). `npm ci && npm run build` on the runner, syncs the built SPA to S3, invalidates the CloudFront cache so you don't see stale assets.
11. **Agent Context** (opt-in, skipped by default). GraphRAG + semantic search over your codebase. Costs ~$800/month idle, so only runs if you set `AGENT_CONTEXT_ENABLED=true` or pass `--agent-context-only`.

**What you'll see:** a progression of `━━━ Step X/11 ━━━` headers, each followed by a short summary. The agent doesn't dump raw Terraform output at you — it tells you "Platform deployed" or "CloudFront provisioned" and moves on.

**What could fail:**

- **EKS Auto Mode pod capacity issues.** First provisioning of a node pool can take 3-5 extra minutes. The agent waits up to 10.
- **Docker Hub rate limits during CodeBuild.** If you see `429 Too Many Requests` pulling `python:3.12-slim`, the fix is already in — the Dockerfile uses `public.ecr.aws/docker/library/python:3.12-slim` (AWS mirror, no rate limit). If it somehow fails anyway, a retry usually works.
- **RDS IAM auth bootstrap.** One-time Kubernetes Job runs `GRANT rds_iam TO bgadmin` against the freshly-created database. If this Job fails, every admin/budget/ratelimit endpoint returns HTTP 500 while `/health` stays 200 — confusing but localized. Fix is to re-run the Job after checking its logs.
- **ALB not appearing within 10 min.** The Kubernetes Ingress controller occasionally takes longer. The agent retries twice before asking you to intervene.

**You can step away.** From step 3 onwards there are no interactive prompts. Come back in ~30 minutes.

---

### Phase 4 — Verification (~2 min, automated)

The agent hits the real endpoints to confirm the stack is healthy:

- `GET /health` via API Gateway → expect 200 with `{"status":"healthy"}`
- `GET /agent/health` unsigned → expect 403 (the AWS_IAM guard is working)
- CloudFront root → expect 200 (the SPA is live)
- A Cognito admin JWT round-trip against `/admin/organizations` → expect 200 (the RDS IAM bootstrap worked)

**What you'll see:** a summary block like:

```
Deployment complete. Status:
- Platform: EKS cluster adp-dev-eks-cluster active, 3 nodes
- Gateway: 2 pods running, /health returns 200
- Frontend: https://d1g6cal2ts4iis.cloudfront.net (200 OK)
- API Gateway: routes via VPC Link v2 → ALB direct
- Database: available, IAM auth working
- Test credentials stored in Secrets Manager at adp/dev/gateway/test-{user,admin}-credentials
- Agent Factory: deployed
```

## After Deploy — What You Have

**An admin dashboard** at the CloudFront URL from the summary. Log in with the test user credentials — you'll find them in Secrets Manager:

```
aws secretsmanager get-secret-value \
  --secret-id adp/dev/gateway/test-admin-credentials \
  --query SecretString --output text | jq
```

**A multi-tenant Bedrock proxy** at the CloudFront URL's `/api/gateway/*` path. Test it by pointing Claude Code or a similar client there — instructions in [`modules/gateway/README.md`](../modules/gateway/README.md).

**Autonomous code agents** that respond to GitHub issue labels on your repo. Label any issue with `agent-operations`, `agent-developer`, or `agent-pm` and an ARC runner spawns, Claude does the work, and a PR lands. This is the Agent Factory's main entry point.

**Full Terraform state in S3** and per-module workflows for iterating — see the next section.

## Day-Two Operations

Once the stack is up, day-to-day work shouldn't touch `deploy-all.sh` again. Use these entry points instead:

| Task | How |
|---|---|
| Change gateway infra (e.g. add an SSM param) | Edit `modules/gateway/infra/`, push, click "Run workflow" on `gateway-infra-apply.yml` in Actions |
| Ship a new gateway backend image | Push changes to `modules/gateway/src/`, the `gateway-deploy.yml` workflow auto-runs |
| Ship a new frontend build | Same as above — `gateway-deploy.yml` covers frontend too |
| Tear down one module for a rebuild | Actions → `<module>-infra-destroy.yml` → type the module name to confirm |
| Full teardown | `./platform/scripts/deploy-all.sh --destroy` (asks for "yes") |
| Delete the Terraform state bucket | `./platform/scripts/bootstrap-destroy.sh` (asks for your account ID) — do this only after module destroys succeed |

## What Doesn't Work Yet

An honest inventory of caveats as of 2026-04-21:

- **First-run verification on a truly clean AWS account hasn't been done** since all the recent fixes landed. This session iterated on an existing dev account, so the logic is tested but the cold path is theoretical. If you hit something unexpected, [file an issue](https://github.com/aws-e/adp/issues/new) — we want that signal.
- **GitHub Apps deletion is manual.** When you undeploy, the three apps stay in your GH org and their credentials stay in Secrets Manager (`adp/<org>/gh-app-*`). Delete via the GitHub UI if you want them gone.
- **The `production` GitHub Actions environment** used to be required; we removed that directive. If you forked before 2026-04-21 and see "environment `production` does not exist" errors, pull main.
- **CloudFront takes 15-20 minutes to fully delete.** Patience.
- **State backend outlives module teardowns.** `deploy-all.sh --destroy` leaves the S3 bucket and DynamoDB table in place so you can redeploy. `bootstrap-destroy.sh` is intentionally separate.

## If You Get Stuck

- Agent stops with a clear error message → follow its instructions. It's likely a missing CLI tool or an AWS permission it can't recover from.
- Agent stops without an error → check `.adp-deploy-state.json` to see which phase was in progress, then re-run the deploy script. State is resumable.
- `kubectl` can't reach the cluster → `aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1`. **The cluster name is `adp-dev-eks-cluster`, not `adp-dev-eks`** — if you see the shorter form anywhere, it's a bug in that place.
- `/health` returns 200 but `/admin/organizations` returns 500 → the RDS bootstrap Job failed. `kubectl logs -n adp-gateway -l app=bedrockgw-dev-rds-bootstrap`, read the log, re-run the job.
- Terraform wants to destroy+recreate the Cognito domain → the Cognito domain uses an account-id suffix for global uniqueness. Make sure you're on post-#52 code.

More detailed runbooks: [`AGENTS.md`](../../AGENTS.md) Troubleshooting section.
