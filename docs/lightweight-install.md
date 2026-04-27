# Lightweight Install (Agents Only)

Run ADP's autonomous code agents (`@agent-developer`, `@agent-pm`, `@agent-architect`, `@agent-product`, `@agent-reviewer`, `@agent-operations`, `@agent-pt-superpower`) on your own AWS + EKS + ARC setup. Label a GitHub issue, an agent picks it up, writes code, opens a PR.

**What this does not give you**: the Bedrock gateway proxy, the chat widget, the user vault, Agent Context (code search / wikis), budget enforcement, or beads task state. For those, see the full-deployment runbook at [`../AGENTS.md`](../AGENTS.md).

Expected active work: **~10-15 minutes**.

The script itself is deliberately minimal — it only configures the fork's GitHub repo state (variables + labels). Everything cluster-side and AWS-side must already be in place before you run it. See "What the script does NOT do" below.

---

## Before you start — what must already be working

**The script assumes your ARC runners can already call AWS APIs.** If `skill-agent` or any other AWS-touching workflow has ever run successfully on your cluster, you're ready.

If your ARC runners have never called AWS, stop and set that up first — that's cluster-admin work (create an IAM role, attach it via IRSA annotation **or** EKS Pod Identity association), not something a per-repo install script should try to do.

### 1. ARC runners with AWS credentials

Your runner pods assume an IAM role (via IRSA or Pod Identity) that can:

- `bedrock:InvokeModel` on the Claude inference profiles
- `secretsmanager:GetSecretValue` on the secret prefix you'll choose below

How to verify without this repo: if your cluster already runs any workflow that calls `aws` and it works, you're fine.

### 2. Fork of this repo

```bash
gh repo fork https://github.com/aws-e/adp --org <YOUR-ORG> --clone
cd adp
gh auth login            # make sure 'repo' scope is granted
gh auth status           # verify 'repo' is in the listed scopes
```

The fork can have any name — the workflows check out the agent source from the upstream by default (this will become configurable — see issue #191). Name matters only for your `<ORG>/<REPO>` references below.

### 3. A GitHub App in your org

Go to `https://github.com/organizations/<YOUR-ORG>/settings/apps/new` and create an App with:

- **Repository permissions**: Issues (Read & write), Pull requests (Read & write), Contents (Read & write), Metadata (Read)
- **Organization permissions**: Projects (Read & write) — **required for `agent-pm`** to create / update GitHub Project boards, and for the `update-board-status` composite action that most agent workflows call. Without this, agents keep working but project board updates silently no-op.
- **Webhook**: uncheck "Active" — agents don't need webhooks
- **Where can this app be installed?**: "Only on this account"

After creating:
1. Note the **App ID** at the top of the page (6-7 digit number).
2. **Generate a private key** (downloads a `.pem` file).
3. **Install App** on the fork: left sidebar → pick your org → "Only select repositories" → select your fork → **Install**.

One App is enough for all three personas (dev / pm / ops). Use three Apps only if you want independent rate limits (5000 req/hr each).

**If you change the App's permissions later** (e.g. add Projects after the fact), GitHub will flag the installation as needing re-approval. Visit `https://github.com/organizations/<YOUR-ORG>/settings/installations` → your App → **Review request** → accept the new permissions. Without this, the new scopes don't actually reach the workflow — the token still carries the old scope set.

### 4. Your GitHub App credentials in AWS Secrets Manager

The script does NOT store these for you — you do it once with the AWS CLI. Pick a prefix (typically `adp/<your-org>`), then:

```bash
PREFIX="adp/<your-org>"           # whatever you want to use
REGION="us-east-1"
APP_ID="<6-7 digit App ID>"
APP_KEY_FILE="/path/to/your-app.private-key.pem"

aws secretsmanager create-secret --region "$REGION" \
  --name "${PREFIX}/gh-app-dev-id" --secret-string "$APP_ID"
aws secretsmanager create-secret --region "$REGION" \
  --name "${PREFIX}/gh-app-dev-key" --secret-string "$(cat "$APP_KEY_FILE")"

# If you're using one App for all personas, just reuse the same values:
for p in pm ops; do
  aws secretsmanager create-secret --region "$REGION" \
    --name "${PREFIX}/gh-app-${p}-id" --secret-string "$APP_ID"
  aws secretsmanager create-secret --region "$REGION" \
    --name "${PREFIX}/gh-app-${p}-key" --secret-string "$(cat "$APP_KEY_FILE")"
done
```

(If you already have equivalents stored at a different prefix, just use that prefix in the next step.)

---

## Run the setup script

```bash
./platform/scripts/lightweight-setup.sh
```

### What it asks

| Prompt | What to type |
|---|---|
| GitHub organization name | Your org (e.g. `acmecorp`) |
| Repository name | Your fork's name (default: `adp`) |
| ARC runner label (used in `runs-on:`) | Your RunnerScaleSet label (default: `arc-runner-org`) |
| Secrets Manager prefix | Where you stored the App secrets (default: `adp/<your-org>`) |
| AWS region | Your region (default: `us-east-1`) |

### What the script does

1. Sets GitHub repo variables so the agent workflows read from your identifiers instead of the upstream `aws-e` defaults:
   - `SECRET_PREFIX` → Secrets Manager prefix where your GitHub App credentials live
   - `ARC_RUNNER_LABEL` → your ARC runner label
   - `BEADS_ENABLED=false` → skip the beads task-state system (not needed for lightweight installs)
   - `AWS_REGION` → only if non-default
2. Creates the agent issue labels on the repo (`agent-developer`, `agent-pm`, etc.).
3. Prints next-step instructions (run a smoke test, verify AWS permissions if you're unsure).

### What the script does NOT do

- Create IAM roles or attach policies
- Annotate service accounts or configure IRSA / Pod Identity
- Restart pods
- Enable Bedrock model access
- Create GitHub Apps or install them on repos
- Store your GitHub App secrets in Secrets Manager (you did that in prereq 4)

These are one-time cluster-admin or browser-only steps. A repo-config script that tried to do them would fail in a different way on every user's cluster.

---

## Verify with a smoke test

File an issue and label it. If everything's wired up, the agent will pick it up within 30-60 seconds, post a "Started" comment, then open a PR.

```bash
gh issue create --repo <ORG>/<REPO> \
  --title "chore: agent smoke test" \
  --body "Verifying the lightweight install." \
  --label agent-developer

gh run list --repo <ORG>/<REPO> --workflow agent-developer.yml --limit 1
gh run watch --repo <ORG>/<REPO> $(gh run list --repo <ORG>/<REPO> --workflow agent-developer.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

If the workflow fails at an AWS API call (`AccessDenied` on Secrets Manager or Bedrock), see the Troubleshooting section below — those errors tell you exactly which permission is missing on the runner's IAM role.

---

## Use the agents

Label any issue with one of:

| Label | What the agent does |
|---|---|
| `agent-developer` | Implement code + tests + open a PR |
| `agent-pm` | Break an epic down into sub-issues |
| `agent-architect` | Write a design doc and commit it |
| `agent-product` | Scope requirements and acceptance criteria |
| `agent-reviewer` | Review an open PR |
| `agent-operations` | Infra / ops work (needs `EKS_CLUSTER` set) |
| `agent-pt-superpower` | Pen test / security review |

Workflow triggers on `issues.labeled` — agent picks it up within 30-60 seconds.

---

## Troubleshooting

### Smoke-test workflow doesn't start

**Symptom**: issue labeled, no workflow run appears.

Causes:
- **Runner label mismatch**: workflow's `runs-on:` is `${{ vars.ARC_RUNNER_LABEL || 'arc-runner-org' }}`. If your ARC RunnerScaleSet uses a different label and `ARC_RUNNER_LABEL` isn't set, the job queues forever. Fix: `gh variable set ARC_RUNNER_LABEL --body '<your-label>' --repo <ORG>/adp`
- **GitHub App not installed on the fork**: check `https://github.com/organizations/<YOUR-ORG>/settings/installations`. Install it on the fork.

### `AccessDenied` calling Secrets Manager

**Symptom**: workflow log shows `AccessDeniedException` on `secretsmanager:GetSecretValue`.

Cause: the IAM role your runners assume doesn't have `secretsmanager:GetSecretValue` on the prefix you configured — OR the secrets don't exist at that prefix yet.

Fix:

```bash
# Check the runner's effective identity (run as a one-off pod using your runner SA)
kubectl run whoami --rm -it --restart=Never \
  --serviceaccount=<runner-sa> -n <runner-ns> \
  --image=public.ecr.aws/aws-cli/aws-cli:latest \
  --command -- aws sts get-caller-identity

# Verify the secret exists at the prefix the workflow will read from
aws secretsmanager describe-secret --secret-id "<prefix>/gh-app-dev-id"
```

If the secret exists and the runner's role ARN is correct, add `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:<region>:<account>:secret:<prefix>/gh-app-*` to that role's policy.

### `AccessDenied` calling Bedrock

**Symptom**: `AccessDeniedException` on `bedrock:InvokeModel`.

Causes (in order of likelihood):
- **Model not available in your region** — Claude Opus 4.6 rolls out region-by-region. Try `us-east-1` or `us-west-2`.
- **Runner's IAM role missing `bedrock:InvokeModel`** — add it to the role, scoped to the foundation-model or inference-profile ARNs you use.

### `404 Not Found` when minting the GitHub App token

**Symptom**: workflow log shows `Error: An installation was not found for this app`.

Causes:
- Wrong App ID stored. Verify: `aws secretsmanager get-secret-value --secret-id <prefix>/gh-app-dev-id --query SecretString --output text` returns a 6-7 digit number matching your GitHub App's page.
- App not installed on the repo. Fix at `https://github.com/organizations/<YOUR-ORG>/settings/installations`.
- Wrong `SECRET_PREFIX`. Fix: `gh variable set SECRET_PREFIX --body 'adp/<correct-org>' --repo <ORG>/adp`.

### `BEADS_S3_BUCKET` error

**Symptom**: workflow fails at the `Setup Beads` step, mentions `adp-beads-state-193832579677`.

Cause: `BEADS_ENABLED=false` not set. The step runs and tries to sync from an S3 bucket in another account.

Fix:

```bash
gh variable set BEADS_ENABLED --body 'false' --repo <YOUR-ORG>/adp
```

### `gh variable set` fails with 403

Cause: `gh auth login` wasn't granted `repo` scope.

Fix:

```bash
gh auth refresh -h github.com -s repo
```

### `agent-pm` can't create a project board / `update-board-status` silently no-ops

**Symptom**: `agent-pm` workflow runs but logs "projectBoardUrl: null" or "project not found" from the `update-board-status` step. Other agent workflows log "Project board not linked to this repo — skipping status update" and proceed normally.

Cause: the GitHub App doesn't have **Organization → Projects: Read & write** permission, or the permission was added after install and the installation hasn't been re-approved.

Fix:
1. Go to your App's settings → **Permissions & events** → under **Organization permissions**, set **Projects** to **Read and write**, save.
2. Visit `https://github.com/organizations/<YOUR-ORG>/settings/installations` → your App shows a **Review request** banner → accept the new permissions.
3. Re-run the workflow. No secret rotation needed — the token now carries the new scope automatically.

If you don't care about project boards, this is harmless — all other agent behavior is unaffected.

---

## Tear-down

```bash
./platform/scripts/lightweight-teardown.sh <YOUR-ORG> <REPO>
```

Removes:
- Repo variables (`SECRET_PREFIX`, `ARC_RUNNER_LABEL`, `BEADS_ENABLED`, `AWS_REGION`)
- Optionally, the agent issue labels (prompts)

Does **not** remove** (and the script never created them, so it can't):
- Secrets Manager entries for your GitHub App. Remove manually if you want:
  ```bash
  for s in gh-app-dev-id gh-app-dev-key gh-app-pm-id gh-app-pm-key gh-app-ops-id gh-app-ops-key; do
    aws secretsmanager delete-secret --secret-id "<prefix>/${s}" --force-delete-without-recovery
  done
  ```
- IAM roles, SA annotations, or Pod Identity associations on your cluster — those are yours to manage.
- The fork itself, or the GitHub App.

---

## Cost

- **Bedrock**: pay-per-use, Opus 4.6 is ~$15/M input + ~$75/M output. Typical `agent-developer` run on a small issue costs $1-4; larger research-heavy runs $5-15.
- **Secrets Manager**: $0.40/mo × 6 secrets = ~$2.40/mo.
- **IAM / STS**: free.
- **EKS / ARC**: whatever you already pay. No baseline added — agents only consume resources while running.

---

## Reference — GitHub Actions variables the workflows read

All variables use `${{ vars.X || 'default' }}` — defaults match today's hardcoded values, so `aws-e/adp` (the upstream) works with nothing set. A forked repo sets the ones it needs to override.

| Variable | Default | Set to | Used by |
|---|---|---|---|
| `ARC_RUNNER_LABEL` | `arc-runner-org` | Your ARC runner label | All agent workflows |
| `SECRET_PREFIX` | `adp/aws-e` | `adp/<your-org>` | All agent workflows |
| `AWS_REGION` | `us-east-1` | Your region | All agent workflows |
| `EKS_CLUSTER` | `bedrockgw-dev-eks-cluster` | Your EKS cluster name | `agent-operations`, `skill-agent` |
| `BEADS_ENABLED` | `true` | `false` | All agent workflows |
| `BEADS_DYNAMODB_TABLE` | `adp-beads-manifest` | _(not set — irrelevant when disabled)_ | All agent workflows |
| `BEADS_S3_BUCKET` | `adp-beads-state-193832579677` | _(not set)_ | All agent workflows |
| `BEADS_DATABASE` | `adp` | _(not set)_ | All agent workflows |

### Secrets Manager layout

Each persona workflow looks up two secrets:

| Secret | Contains |
|---|---|
| `<SECRET_PREFIX>/gh-app-dev-id` | App ID for the dev persona (`agent-developer`, `agent-architect`, `agent-product`) |
| `<SECRET_PREFIX>/gh-app-dev-key` | Private key (PEM) for the dev persona |
| `<SECRET_PREFIX>/gh-app-pm-id` + `-key` | App credentials for `agent-pm` |
| `<SECRET_PREFIX>/gh-app-ops-id` + `-key` | App credentials for `agent-operations`, `agent-reviewer`, `agent-pt-superpower`, `skill-agent` |

If you want a single-App install, store the same App ID and key under all three prefixes — the `lightweight-setup.sh` script does this for you when you answer `N` to "Use separate Apps".

### Which workflow reads which variables

| Workflow | ARC_RUNNER_LABEL | SECRET_PREFIX | AWS_REGION | EKS_CLUSTER | BEADS_ENABLED |
|---|---|---|---|---|---|
| `agent-developer.yml` | ✓ | dev | ✓ | – | ✓ |
| `agent-pm.yml` | ✓ | pm | ✓ | – | ✓ |
| `agent-operations.yml` | ✓ | ops | ✓ | ✓ | ✓ |
| `agent-architect.yml` | ✓ | dev | ✓ | – | ✓ |
| `agent-product.yml` | ✓ | dev | ✓ | – | ✓ |
| `agent-reviewer.yml` | ✓ | ops | ✓ | – | ✓ |
| `agent-pt-superpower.yml` | ✓ | ops | ✓ | – | ✓ |
| `skill-agent.yml` | ✓ | ops | ✓ | ✓ | – |

---

## Got stuck?

If a step above failed and the troubleshooting section didn't cover it, file an issue in your fork with:
- The step that failed
- The exact error message
- Output of `gh variable list --repo <ORG>/adp`
- Output of `aws sts get-caller-identity`
- Output of `kubectl get sa <runner-sa> -n <runner-ns> -o yaml`
