# Lightweight Install (Agents Only)

Run ADP's autonomous code agents (`@agent-developer`, `@agent-pm`, `@agent-architect`, `@agent-product`, `@agent-reviewer`, `@agent-operations`, `@agent-pt-superpower`) on your own AWS + EKS + ARC setup. Label a GitHub issue, an agent picks it up, writes code, opens a PR.

**What this does not give you**: the Bedrock gateway proxy, the chat widget, the user vault, Agent Context (code search / wikis), budget enforcement, or beads task state. For those, see the full-deployment runbook at [`../AGENTS.md`](../AGENTS.md).

Expected active work: **~20-30 minutes**.

---

## Before you start — 4 things you must have ready

If any of these aren't true, fix them first. The setup script will fail otherwise.

### 1. AWS account

```bash
aws sts get-caller-identity
```

Expected: account ID + ARN printed. If this fails, run `aws configure` or set `AWS_PROFILE`. Claude models on Bedrock are available by default in supported regions — no model-access request needed.

### 2. EKS cluster with ARC runners installed

```bash
kubectl cluster-info
kubectl get pods -A | grep -i runner
```

Expected: cluster info printed, at least one ARC runner pod in `Running`. If ARC isn't installed, follow the [ARC quickstart](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners-with-actions-runner-controller/quickstart-for-actions-runner-controller) first.

Note down:
- **Cluster name** (e.g. `my-eks`)
- **Runner namespace** (e.g. `arc-runners`)
- **Runner service account** — `kubectl get sa -n <runner-namespace>`
- **Runner label** used in `runs-on:` (e.g. `arc-runner-org`) — check your RunnerScaleSet spec

### 3. GitHub App in your org

Go to `https://github.com/organizations/<YOUR-ORG>/settings/apps/new` and create a new App.

Required settings:
- **Permissions**: Issues (Read & write), Pull requests (Read & write), Contents (Read & write), Metadata (Read)
- **Webhook**: uncheck "Active" — agents don't need webhooks
- **Where can this app be installed?**: "Only on this account"

After creating:
1. Note the **App ID** shown at the top of the App's page (6-7 digit number)
2. Under **Private keys**, click **Generate a private key** → downloads a `.pem` file
3. **Install App** in the left sidebar → pick your org → "Only select repositories" → select your fork of this repo → **Install**

For a minimal setup, one App is enough for all personas. For production, create three (dev / pm / ops) for independent rate limits.

### 4. Fork of this repo + CLI auth

```bash
gh repo fork https://github.com/aws-e/adp --org <YOUR-ORG> --clone
cd adp
gh auth login           # make sure 'repo' scope is granted
gh auth status          # verify 'repo' in the listed scopes
```

Also need installed locally: `aws`, `gh`, `kubectl`.

---

## Run the setup script

```bash
./platform/scripts/lightweight-setup.sh
```

### What it asks you

| Prompt | What to type |
|---|---|
| GitHub organization name | Your org (e.g. `acmecorp`) |
| Repository name | `adp` |
| AWS region | `us-east-1` (or wherever you want) |
| EKS cluster name | From prereq 2 |
| ARC runner K8s namespace | From prereq 2 |
| ARC runner service account name | From prereq 2 |
| ARC runner label (used in `runs-on:`) | From prereq 2 |
| Secrets Manager prefix | Accept default (`adp/<your-org>`) |
| GitHub App ID for dev persona | Numeric ID from prereq 3 |
| Path to GitHub App private key (.pem) | Absolute path to your `.pem` |
| Use separate Apps for pm and ops? | `N` for a first install |

Review the summary. Type `y` to proceed.

### What the script does

1. Validates prerequisites (`aws`, `gh`, `kubectl` installed + authenticated).
2. Creates IAM role `adp-agent-runner-role` with trust policy scoped to your EKS OIDC provider + runner service account, inline policy for `bedrock:InvokeModel` + `secretsmanager:GetSecretValue` on your prefix.
3. Stores GitHub App credentials at `<prefix>/gh-app-{dev,pm,ops}-{id,key}` (6 secrets).
4. Annotates your ARC runner service account with the role ARN so IRSA kicks in.
5. Restarts only the runner pods using that service account (scoped with a field selector — other workloads in the namespace are untouched).
6. Sets GitHub repo variables: `SECRET_PREFIX`, `ARC_RUNNER_LABEL`, `EKS_CLUSTER`, `BEADS_ENABLED=false` (and `AWS_REGION` if non-default).
7. Creates the agent issue labels (`agent-developer`, `agent-pm`, etc.).
8. Files a smoke-test issue and tails the workflow run.

---

## Verify the smoke test

The script files an issue titled `chore: lightweight install smoke test` and prints the URL. Within 2-3 minutes:

- A comment appears from your GitHub App's bot user.
- A workflow run appears under Actions → `@agent-developer - Developer`.
- The agent posts a follow-up comment or opens a draft PR.

Watch via CLI:

```bash
gh run list --repo <YOUR-ORG>/adp --workflow agent-developer.yml --limit 1
gh run watch --repo <YOUR-ORG>/adp $(gh run list --repo <YOUR-ORG>/adp --workflow agent-developer.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

If the smoke test passes, close the issue and start labeling real issues.

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

Cause: the runner pod didn't pick up the IRSA annotation (still has an older identity).

Fix:

```bash
kubectl get sa <runner-sa> -n <runner-ns> -o yaml | grep role-arn
# should show: eks.amazonaws.com/role-arn: arn:aws:iam::<ACCT>:role/adp-agent-runner-role

kubectl delete pods -n <runner-ns> --field-selector spec.serviceAccountName=<runner-sa>
```

ARC will recreate the pods with the correct credentials.

### `AccessDenied` calling Bedrock

**Symptom**: `AccessDeniedException` on `bedrock:InvokeModel`.

Causes (in order of likelihood):
- **Model not available in your region** — Claude Opus 4.6 rolls out region-by-region. Try `us-east-1` or `us-west-2`.
- **IRSA didn't apply to the runner pod** — same fix as the Secrets Manager case above.

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

---

## Tear-down

```bash
./platform/scripts/lightweight-teardown.sh <YOUR-ORG> adp adp/<YOUR-ORG> us-east-1
```

Removes:
- IAM role `adp-agent-runner-role` + inline policy
- All 6 Secrets Manager secrets
- The 5 GitHub repo variables

Does **not** remove:
- The runner SA annotation (remove manually: `kubectl annotate sa <runner-sa> -n <runner-ns> eks.amazonaws.com/role-arn- --overwrite`)
- The fork, the GitHub App, or the issue labels

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
