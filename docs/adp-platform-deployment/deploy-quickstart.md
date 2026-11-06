# Install and upgrade ADP

For interrupted runs and explicit recovery flags, see [deployment recovery](deployment-recovery.md).

Use this guide to deploy ADP into your own AWS account. Run commands from the
repository root. An AI agent should also follow the
[agent deployment instructions](deploy-with-agent.md), including confirmation
of the target AWS account before deployment.

## 1. Prepare

You need an AWS profile with permission to provision the platform, AWS CLI v2,
Git, Python 3.12+, Terraform 1.14+, kubectl, Node.js 22+ and npm. Release selection
also needs the GitHub CLI (`gh`) authenticated with read access to `aws-e/adp`.
Docker is required when using `--local` builds; the default uses AWS CodeBuild.
The underlying preflight checks the remaining requirements for your scope.

Deployment scripts require **Bash 4.4+ and `flock`**. On macOS, install them
with `brew install bash flock`, then put `$(brew --prefix bash)/bin` first in
`PATH` so both the launcher and its child scripts use Homebrew Bash. Stock
macOS Bash 3.2 is rejected before deployment starts, including for `--dry-run`.

```bash
gh auth status
gh repo clone aws-e/adp
cd adp

aws sts get-caller-identity --profile customer \
  --query '{Account:Account,Arn:Arn}' --output table
gh release list --repo aws-e/adp
```

Replace `customer` with your AWS profile and `v1.2.0` below with an existing
published GitHub Release tag. The launcher changes must be present in your
checkout. Release selection is covered by offline tests; those tests are not
live deployment acceptance for a published version.

`--aws-profile` overrides inherited profile and credential environment variables
for this invocation. If omitted, normal AWS credential discovery applies.
See [profile and release details](platform_upgrades.md) if your named profile
uses `credential_source = Environment`.

The examples use Terraform environment `dev` in `us-east-1`. The AWS profile
selects the account; `--env` selects resource names within that account. For
example, a demo account can still use `--env dev`.

Review the selected environment overlays before provisioning a new account.
Published-release deployment automatically selects portable configuration.
Bare source deployment can inherit checked-in `dev` bindings and worker
activation attestations; changing account IDs does not make these valid for a
customer deployment. Follow the
[environment overlay checks](deploy-with-agent.md#check-environment-overlays-before-provisioning).

## 2. Install or upgrade

```bash
# First installation into a new account (use your real operator email):
export ADP_BOOTSTRAP_ADMIN_EMAIL='operator@your-company.example'
./deploy.sh --aws-profile customer --release v1.2.0

# Upgrade an existing deployment:
./deploy.sh --aws-profile customer --update --release v1.2.0

# Preview release selection and AWS identity without deploying:
./deploy.sh --aws-profile customer --release v1.2.0 --dry-run
```

Add `--update` to the preview command to preview an upgrade. A dry run does not
create a Terraform plan or prove that the account is ready to deploy.

The launcher fetches the release's exact source into a clean checkout. A fresh
install bootstraps the Terraform state backend and runs the full deployment.
An upgrade uses the existing backend and guarded update procedure. **An upgrade
does not fall back to a fresh installation when prerequisites are missing.**

Fresh installs use an [operator-selected administrator](bootstrap-administrator.md)
and a Cognito temporary-password invitation. Portable releases do not create
shared test accounts. Updates preserve existing administrator configuration.

Release upgrades also retain the target's chat and assistant UI enablement and
model-root bindings. For older releases that omitted these inputs from their
saved configuration, the upgrader recovers the target's Terraform-managed
parameters. Fresh installations keep the module defaults until those settings
are configured for the account.

Both paths build from the selected release's source in the target account;
they do not consume prebuilt GitHub release assets. The original checkout stays
untouched. The printed working directory contains the deployment journal and a
`release-install.json` or `release-upgrade.json` receipt. Keep these private and
retain them for diagnosis.

Common options:

| Option | Purpose |
|---|---|
| `--env dev --region us-east-1` | Select environment and AWS region; retain the existing values for upgrades. |
| `--skip-agents` | Select platform + gateway only; this is a partial deployment. |
| `--local` | Build container images with local Docker. |
| `--anthropic-use-case FILE` | Supply real organization details if Bedrock first-use registration is needed. |

Bedrock access preparation and verification run during deployment. If first-use
registration or an organization Marketplace policy blocks access, follow
[Bedrock readiness](bedrock-first-run.md).

Prefer the published-release path for customers: it prepares portable
configuration in an isolated checkout automatically. For a fresh installation
from a reviewed unpublished commit, create a new isolated checkout and prepare
portable defaults **before** adding target overrides or retained-resource imports:

```bash
# Only in a new isolated checkout, before target customization:
python3 platform/scripts/prepare-release-config.py \
  --root "$PWD" --env dev --region us-east-1

# Review/customize target inputs before deploying:
ADP_PORTABLE_RELEASE_CONFIG=true ./deploy.sh \
  --aws-profile customer --env dev --region us-east-1
```

Preparation replaces platform, gateway and webhook environment `.tfvars`; it
is not an operation to run over existing customer configuration. Read the
[source preparation details](deploy-with-agent.md#select-the-source-and-install-mode)
for backup location, JSON overlay restrictions and retained-resource handling.
The source launcher uses the current checkout and includes bootstrap.

For a direct source upgrade, retain its reviewed target-specific configuration:

```bash
./deploy.sh --aws-profile customer --env dev --region us-east-1 --update
```

See [advanced upgrades](platform_upgrades.md) for module scope, retained inputs,
plan gates and recovery. Do not rerun manual phases after a successful full install.

### Chat mode

Step 10 decides what happens to Chief-of-Staff chat (`--chat`, or
`ADP_CHAT_MODE`; passed through by `deploy.sh`). The default, `--chat retire`,
is today's behaviour: the legacy credentialed `chat-agent-worker` is drained and
removed and chat becomes unavailable. `--chat keep-legacy` leaves that worker
running and skips every retirement step (the journal records that a credentialed
worker remains). `--chat delegated --chat-exception <record>` deploys delegated
chat (supervisor + sandbox ScaledJob with Kiro Crew in the sandbox image) with
no GitHub Actions environment: both images are built, the sandbox gateway TLS
endpoint is provisioned (`platform/scripts/apply-chat-tls.sh`; a dev-only
issuer is generated into Secrets Manager `adp/<env>/chat-sandbox-gateway/issuer`
when none exists), the ScaledJob and Crew controls are applied, and the legacy
worker is retired only after the delegated ScaledJob is Ready. Delegated chat is
gated on the #6937 qualification, so the record reference is mandatory and is
written to the deployment journal; the run refuses before any build without it.

```bash
./deploy.sh --aws-profile customer --env dev --region us-east-1 --update \
  --chat delegated --chat-exception "#6937 evidence <id>"
```

### Dev Box (optional module)

The EC2 developer desktop is not part of the core platform. `--dev-box on` (or
`ADP_DEV_BOX=on`; passed through by `deploy.sh`) runs its stage after the agent
factory; `--dev-box-only` adds it to an existing environment on its own. Both call
`modules/user-services/dev-box/scripts/deploy-dev-box.sh`, which needs
`environments/<env>/modules/dev-box.tfvars` and `dev-box-platform.tfvars` (dev ships
them with `stage_enabled = true`, so dev runs the stage by default; `--dev-box off`
skips it). Without the module nothing changes. See the "Dev Box" sections of
[deployment-reference.md](deployment-reference.md).

```bash
./deploy.sh --aws-profile customer --env dev --region us-east-1 --update --dev-box-only

# Upgrade a published release while leaving Dev Box uninstalled and chat retired:
./deploy.sh --aws-profile customer --env dev --region us-east-1 --update \
  --release v1.2.0 --dev-box off --chat retire
```

## 3. Sign in and connect GitHub

Full deployment includes the gateway, frontend, broker, first administrator,
agent factory and webhook stack. Use the dashboard URL and administrator setup
reported by the deployment.

**GitHub App setup comes after installation.** As `platform_admin`, open
Settings → Connections → **Set up GitHub App**, then complete the browser flow
and installation. Existing upgrades preserve the App configuration. Source
repository access through `gh` is separate from this runtime App connection.

## 4. Verify and recover

Check the deployment's exit status and receipt. Verify the dashboard loads and
its `/api/health` response contains `{"status":"healthy"}`; an HTTP 200 alone
can be the frontend fallback page. Full deployment also requires ready factory
workers. Use the [phase reference](deployment-reference.md) and
[resource manifest](deployment-manifest.md) for detailed checks.

A real GitHub issue-to-agent round trip is a separate smoke test after App setup;
it posts to GitHub and runs a model. Agents need explicit authorization for that
external test.

If a run fails, retain its output and journal, diagnose the failed phase, and
retry the same version after resolving the cause. Do not automatically add
`--confirm-destructive` to bypass an upgrade refusal. See
[upgrade recovery](platform_upgrades.md#8-failure-handling-and-rollback).
Deploying older code is not an automatic database rollback.

## Remove a deployment

Preview teardown and validate the destroy plans:

```bash
./platform/scripts/undeploy.sh --aws-profile customer --dry-run
```

After reviewing it, run without `--dry-run` and follow its account confirmation.
The Terraform backend, GitHub credentials and their encryption key survive by default.
Teardown stops at the first failure; retain its private evidence and rerun after
resolving the reported cause. See the
[teardown reference](deployment-reference.md#teardown) before removing those.

Before reinstalling after teardown, reconcile retained resources with Terraform
state; retained resources detached from state may require reviewed imports. See
[agent reinstall guidance](deploy-with-agent.md#reinstall-after-teardown).

For maintainers publishing prebuilt artifacts through integration-test and
pre-production, use [internal release promotion](release-promotion.md). That
pipeline is separate from customer GitHub Release selection.
