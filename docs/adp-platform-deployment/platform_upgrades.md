# Platform Upgrades

For the build-once integration-test → approved pre-production path, see
[Release promotion](release-promotion.md).


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

To upgrade from a published GitHub Release:

```bash
./deploy.sh --aws-profile customer-test --update --release v1.2.0

# Resolve the release and display the AWS target without deploying:
./deploy.sh --aws-profile customer-test --update --release v1.2.0 --dry-run

# First installation into a new account (omit --update):
./deploy.sh --aws-profile customer-test --release v1.2.0
```

Replace `v1.2.0` with an existing release's exact tag in `aws-e/adp`.
The launcher requires Python 3, Git, the GitHub CLI (`gh`) and AWS CLI,
in addition to the normal upgrade prerequisites. Authenticate `gh` with access
to the repository (`gh auth login`, or `GH_TOKEN` for automation). Private
repository users need read access to download the source. A bare Git tag or a
draft release is not sufficient; explicitly named published prereleases are
accepted. There is no implicit `latest` selection.

The launcher resolves the release tag to its full commit SHA, fetches a clean
checkout, verifies that the tag still points to that commit, and runs that
version's `platform/scripts/deploy-all.sh --update`. `--aws-profile` selects a
named AWS profile for every child process, overriding inherited profile and
static/web-identity credential environment variables. Profiles that depend on
`credential_source = Environment` need those credentials and should use the
existing `AWS_PROFILE` environment interface instead. When `--aws-profile` is
omitted, credentials/profile are inherited unchanged. The flag also works for
fresh deployments and updates without a release.
`--env`, `--region`, `--local` and `--skip-agents` are supported;
`--skip-agents` selects a gateway-only deployment. Without `--update`, release
selection performs a fresh installation: it runs the selected release's
`bootstrap.sh` to prepare the Terraform backend, then `deploy-all.sh` in fresh
mode. Bootstrap failure stops the installation. Full installs include the
required agent factory and webhook stack; GitHub App setup follows deployment
via Settings → Connections. `--anthropic-use-case FILE` supplies real organization
registration details if Bedrock first-use registration is needed; relative file
paths are resolved before entering the release checkout. Update mode never
falls back to fresh installation when its prerequisites are missing.
Without `--release`, `./deploy.sh --update` upgrades from the current
checkout using the same underlying script.

This path **builds from release source in the target account**. It does not
consume GitHub release assets or the internal immutable artifact manifest.
It supports customer accounts without setting up the internal promotion
pipeline. Use protected tags and never move a published version to new code.
Publish a new release for each fix. Selecting older code is not an automatic
database rollback; check migration compatibility before attempting recovery.

Published customer releases use `config/release-defaults/` for portable platform,
gateway and webhook inputs. The launcher materializes those defaults only in
its isolated release checkout; `environments/dev/` remains the internal
platform-account configuration and is not copied into customer release inputs.
The existing foreign-account checks and saved-plan gates remain enforced.

Upgrades overlay the target's recovered state and retained `release_configuration`
outputs on those defaults. These sensitive outputs record private deployment inputs for
subsequent releases; worker images remain selected by the release. Legacy basic
installations retain discovered integration, database, engine, cluster access
and encryption settings. An older installation with activated Task API or
protected worker settings but no retained input snapshot must first perform a
reviewed direct update using its original target-specific configuration. The
release path refuses to guess those activation settings or turn them off.

RC1 predates this separation and fails its account-reference check on customer
accounts. Use the corrected release candidate and an updated launcher. Its
release tag is retained unchanged for traceability.

The original working tree is untouched. A private temporary directory retains
the selected checkout, deployment journal and `release-upgrade.json` (or
`release-install.json`) receipt with the release tag, source SHA, account, mode,
scope and completion/failure status.
Its path is printed; retain it for diagnosis and remove it manually when no
longer needed. Treat it as sensitive deployment output. Local configuration is
not copied into the checkout: installed agent-context still requires its
original configuration, so use the direct upgrade path below when that applies.
A dry run checks release metadata and AWS identity only; it is not a Terraform
plan or a full readiness check.

To publish a version, merge and validate the intended source, create its tag on
that commit, then publish a GitHub Release for that tag. Users need a checkout
containing this updated launcher; the selected release must contain an
update-capable `deploy-all.sh`. Publishing a GitHub Release does not itself
deploy any account.

For direct upgrades or advanced scope/skip flags:

```bash
# From a clean checkout of the code you want to deploy (main or a pinned tag):
git pull origin main            # or: git checkout <tag>

# Credentials must resolve to the TARGET account, not your default profile:
aws sts get-caller-identity --query Account --output text   # verify!

# Run the upgrade:
./platform/scripts/deploy-all.sh --update
```

Select an account with `AWS_PROFILE`; `--env` and `--region` select its deployed
environment and region:

```bash
AWS_PROFILE=customer-test ./platform/scripts/deploy-all.sh --update --env dev --region us-east-1
```

Update mode discovers the platform, gateway, webhook, agent-factory and
agent-context states in that account. **Agent factory is required for a full
platform deployment.** A full upgrade includes it even when an older deployment
omitted its state. Agent-context remains optional and is upgraded only when
already installed. Explicit scope flags select a partial maintenance operation;
they do not certify that the entire platform is deployed.

When factory state is missing, preflight requires existing gateway and
webhook-ingress states and prepares an additive factory installation through
the same saved-plan gate. It uses the existing broker's single allowed GitHub
organization as the legacy secret namespace. An empty or multiple-org
allowlist leaves legacy GitHub integration unconfigured; the factory still
installs without upfront GitHub setup. `ADP_GITHUB_ORG` can explicitly select
that namespace when needed. It does not infer a customer's org
from this repository's remote, register an App, enable ARC, copy the platform
account's installation ID, or overwrite the gateway's registry seed.
Existing factory installations keep their recovered configuration.
The upgrade also retains any runner role owned by factory state. When that
role is missing, it checks for legacy IAM name collisions and selects a
dedicated factory role without importing or changing the existing role. This
check also applies when retrying a partially installed factory.

Completion requires factory Terraform state, a Ready agent-gateway ScaledJob,
the Ready chat worker and its image-prepull DaemonSet, and the intended release
images. The factory stage bundles ingest Lambda dependencies and the real
session-cleanup handler before Terraform applies; it publishes both worker
images. Python uses the source SHA tag, while chat uses `<SHA>-chat` in the same
repository so the two builds cannot overwrite each other. Missing state or failed readiness stops the
upgrade instead of printing a success message. Missing prerequisite states or
resources created outside Terraform require recovery before installation;
the upgrade does not force-create or replace those resources.

Scope/skip flags restrict the selected work.
It retains existing platform-managed EKS admin principals and public access
CIDRs, adds the operator's CIDR if needed, and waits for the EKS access update
before using kubectl. Private-only endpoints stay private and require existing
network reachability.

Existing ECR repositories retain their encryption type and KMS key. ECR cannot
change these settings in place, so applying the key used by new deployments to
older repositories would otherwise propose deleting their images and replacing
the repositories. Newly added repositories use the dedicated managed ECR key.

The run saves state snapshots and account-specific variable overrides in a
private `adp-upgrade-<account>.*` directory (printed at startup). These files can
contain sensitive Terraform state: do not commit or publish them. Overrides
take precedence over shared tfvars for preserved settings; deliberate changes
to those settings belong in a separate reviewed configuration change.

### Existing GitHub integration

An upgrade does not register a new GitHub App or rotate its credentials. It
preserves the existing broker allowlist, organization, ARC repository and
installation ID, custom frontend hostname/certificate, and optional GitLab
origin. It snapshots current secret **version IDs** without fetching secret
values, and existing installation-to-tenant mappings. After deployment it checks
that those versions, mappings, broker settings and integration URLs are still
present and unchanged. Added installations are allowed. Changing existing
credential material or deleting an identity table is blocked by the plan gate,
including when `--confirm-destructive` is supplied.

This check proves configuration preservation, not an issue-to-PR round trip.
The latter requires a separate authorized GitHub smoke test. An environment
without an App stays unconfigured. No browser setup is required for an existing
App installation.

### Additional cluster capacity subnets

When the cluster's original private subnets run out of IP addresses, the CNI
fails every newly scheduled pod with `failed to assign an IP address to
container`, and existing pods keep running while nothing new can start. The
supported remedy on a running Auto Mode cluster is to add **already-existing**
private subnets to the cluster's own subnet set: the AWS-managed `default`
NodeClass takes its subnets from the cluster's `resourcesVpcConfig.subnetIds` and
exposes no selector of its own, so widening that set is what gives new nodes
addresses. Do not edit the managed NodeClass — Auto Mode reconciles such edits
away.

Subnet IDs are account-specific, so they are never committed. Supply them as a
map keyed by the availability zone each subnet is in:

```bash
export TF_VAR_additional_private_subnet_ids_by_az='{"us-east-1a":"subnet-...","us-east-1b":"subnet-..."}'
```

For CI applies, set the `ADDITIONAL_PRIVATE_SUBNETS_BY_AZ` repository variable to
the same JSON object. It is **not** passed straight through, because on a cluster
that has already been widened that would not be a no-op: the variable's default
is `{}`, so an unset or stale variable plans the added subnets away and re-breaks
pod IP assignment for every node launched afterwards. Unset and "deliberately
none" are indistinguishable at the variable, so the effective map is resolved
against the live cluster instead — unset or blank retains what the cluster has, a
map that omits a live addition is refused, naming more is allowed, and narrowing
requires the `ALLOW_CAPACITY_SUBNET_REMOVAL` authorisation. A bare
`terraform apply` with the variable unset gets none of this protection.

The change is additive — the original subnets always stay in the set — and it
creates nothing: no subnet, NAT gateway or VPC endpoint. It widens the
**cluster's** subnet set only; the RDS subnet group, load balancers, Lambda VPC
configurations and VPC endpoints are unaffected. Existing nodes are not moved,
so only nodes launched after the apply can use the added capacity.

Every entry is checked before anything is applied. The plan **fails** if a subnet
is not in the platform VPC, is not really in the zone it is keyed by, assigns
public IPs, has no default route, or reaches `0.0.0.0/0` through an internet
gateway rather than NAT. Keying by zone is what makes one-subnet-per-zone
structural: a subnet pasted under the wrong zone is refused instead of quietly
collapsing the added capacity into a single zone.

Upgrades retain this. An `--update` run rediscovers the live cluster's subnet set
and re-exports the additions Terraform does not own, sharing the rules above via
`platform/scripts/capacity_subnets.py`, so a later routine update cannot silently
shrink the set back and re-break pod scheduling. If retention cannot represent or
account for what it finds — a subnet whose zone it cannot resolve, two additions
in the same zone, or a live cluster subnet Terraform manages that is not one of
the networking private subnets — it stops rather than dropping them. A failed AWS
read is likewise never read as "no additions". Note that `platform.tfvars.json`
is applied after the repository tfvars, so during an update run configure
additions through the export above (which discovery merges in), not by editing
`environments/<env>/platform.tfvars`.

Rolling this out on an existing cluster has a prerequisite: a saved targeted plan
must be renderable as JSON for review, which the provider/schema mismatch in
#5831 currently prevents on `dev`. Use the sequence in
[`docs/runbooks/eks-pod-ip-exhaustion.md`](../runbooks/eks-pod-ip-exhaustion.md)
§5 — a reviewed scoped saved plan with resolved inputs — not an ordinary full
apply.

### Ordered upgrades and completion

Legacy webhook-secret KMS ownership moves from webhook state to platform state
before the first apply. The script imports and verifies destination ownership
before removing source tracking; the AWS key and alias stay unchanged. It also
removes obsolete duplicate tracking of the gateway DynamoDB alias after checking
gateway ownership. Conflicting key IDs stop the migration for recovery. Keys
retained during recovery stay managed with deletion protection.

On clusters where network-policy enforcement is not yet active, the initial
platform pass defers activation. Webhook infrastructure installs the collector
egress policy first; the final platform pass audits DNS and HTTPS allowances
before activation. Already-active enforcement is retained.

All gateway passes preserve both ALBs. A final reconciliation repairs resources
that API/ALB controllers removed during earlier passes, followed by a plan that
must report no remaining changes. Frontend publishing uses the standalone
publisher, includes both account-connection templates, and runs after optional
chat endpoints have been provisioned. CDN health and GitHub preservation checks
must pass before success is reported.

Agent-context still needs its original `modules/agent-context/config.local.env`
when that module is installed. Preflight requires that file and checks its
cluster and region before any apply; use `--skip-agent-context` when upgrading
only the other components. Its application deploy runs with `--skip-terraform`
so nested scripts cannot bypass the update gate.

The script plans each module before applying it, builds SHA-tagged images,
rolls out the backend, runs database migrations on the intended release, and
verifies health. CodeBuild image builds and the pricing-refresh drain window
also run on repeat upgrades, so allow time for a full deployment even when
Terraform has little or no drift.

**What stops the run:** failed validation, protected integration changes, or
unrecognized Terraform deletes/replacements. See §5 for the narrowly defined
routine deployment replacements that proceed automatically.

---

## 2. What "update mode" is (and is not)

### Installations that predate releases

An original release tag is not required. The upgrade uses existing Terraform
state, live configuration and the installed database schema. Do not wipe a
deployment merely because it predates the release process. Before a substantial
version jump, retain the previous image digest, obtain a database recovery point,
record the Alembic revision and rehearse migrations on a restored database.
Database compatibility still needs live acceptance; offline upgrade tests do not
prove that every historical schema or customer data set can migrate successfully.

Use Bash 4.4+ and `flock` (see the quickstart). The launcher checks these before
loading deployment configuration or contacting AWS.

Compatibility preflight runs before Marketplace agreement preparation, EKS CIDR
updates and Terraform applies. It checks operator access ownership, ADP-owned
account settings and the engine. An inaccessible or unhealthy existing engine is
an error; only a positively identified missing function, absent from gateway
state, is eligible for installation. A live engine outside Terraform state needs
review and import, not replacement.

When the engine is missing, the platform stage creates its build prerequisites,
then the gateway image is built and resolved to an immutable digest **before**
the first gateway plan. The engine is created with its schedule disabled. All
pre-final gateway Terraform passes hold that schedule disabled while the gateway,
database migrations and selected worker stages run. Final gateway reconciliation
restores the desired schedule state. An intentionally disabled schedule stays
disabled. Existing engines are paused and drained for their configured maximum
invocation timeout before gateway infrastructure changes. A failure before
finalization leaves the schedule disabled; retry the
same checkout with `--resume` after correcting the failure. A new run after an
incomplete apply conservatively retains a disabled schedule when the original
desired setting cannot be recovered; review it explicitly after recovery.

Cluster bootstrap-admin permissions are preserved as an immutable creation-time
setting, including installations created with either `true` or `false`. The
upgrade plan gate rejects deletion/replacement or removal from state of EKS
clusters, OIDC providers, RDS databases/clusters and ECR repositories even with
`--confirm-destructive`. Intentional infrastructure replacement is a separate
reviewed migration, not a code upgrade.

### Account-wide settings and ownership

Portable release installs default `manage_ecr_registry_scanning` and
`manage_bedrock_invocation_logging` to `false`. Repository-level ECR scanning
remains configured; the account/region registry configuration is left to its
existing owner. Direct Terraform configurations retain their existing defaults;
set ownership explicitly for a new organization-managed account.

Upgrades derive ownership from current platform state, so a new default does not
delete existing ADP logging. Partial Bedrock installs retain their supporting
bucket, key, role and log group without trying to create an unowned invocation
logging configuration. The retained ownership values are saved for subsequent
portable upgrades. Enabling ownership for the first time requires a separate
reviewed configuration apply; upgrades do not silently adopt account singletons.

Preflight refuses to downgrade an ADP-tracked registry that now uses ENHANCED
scanning, or to overwrite Bedrock destinations that differ from tracked state.
Read/permission errors also stop the run. Confirm ownership with the account
administrator. Do not treat access denied as proof that configuration is absent.

To hand an existing singleton to an external owner, first initialize the correct
platform backend, verify the AWS account and region, and save a **fresh private
`terraform state pull` backup**. Inspect `terraform state list` and the live
configuration. After ownership review, remove only the singleton's tracking:

```bash
# In platform/infra, using the verified customer backend and AWS profile.
# Use exactly the ECR address present in state: older releases lack [0].
terraform state rm 'module.ecr.aws_ecr_registry_scanning_configuration.main'
# Or, for the indexed address introduced by the ownership switch:
# terraform state rm 'module.ecr.aws_ecr_registry_scanning_configuration.main[0]'

# If relinquishing Bedrock invocation configuration, retain its destinations:
terraform state rm 'module.bedrock_invocation_logging[0].aws_bedrock_model_invocation_logging_configuration.this[0]'
```

These are deliberate state migrations; run only the command for the setting
being handed over. They do not call the AWS configuration deletion APIs. Set
`manage_ecr_registry_scanning=false` in the target configuration. For an existing
Bedrock support module, retain `manage_bedrock_invocation_logging=true` and set
`bedrock_invocation_logging_enabled=false`. If no Bedrock module resources exist,
set `manage_bedrock_invocation_logging=false`. The update preflight derives these
same values from current state, including on a resumed run. Verify that the next
plan contains no singleton create/delete and that the external configuration is
unchanged. Retain unused destinations until their retention and cleanup needs
have been reviewed separately.

Simply setting a counted resource/module to false can schedule destruction.
The upgrade gate blocks singleton deletion even with `--confirm-destructive`;
direct Terraform applies do not use that gate. Do not remove a whole logging
module from state merely to relinquish its account-wide configuration.

### Operator access

Preflight resolves an assumed SSO session to its full IAM role ARN, including
the role path. If its EKS access entry or cluster-admin association already exists
outside platform state, the upgrade stops before applying resources and identifies
the import needed. Verify that this is the intended deployment administrator and
that another state does not own the entry. Import both matching objects when
appropriate, using the account-specific variable files and these provider IDs:

| Terraform address | Import ID |
|---|---|
| `module.eks.aws_eks_access_entry.admins["<IAM-role-ARN>"]` | `<cluster-name>:<IAM-role-ARN>` |
| `module.eks.aws_eks_access_policy_association.admins["<IAM-role-ARN>"]` | `<cluster-name>#<IAM-role-ARN>#<cluster-admin-policy-ARN>` |

An absent API access entry is not automatically an error: older clusters can grant
access through the creator or `aws-auth`. The subsequent Kubernetes check must
succeed. If the operator lacks access, have an existing cluster administrator
grant the intended deployment access, then reconcile its Terraform ownership
before retrying. Do not adopt namespace-scoped entries as cluster administrators.

For a partially upgraded environment, use the **current** states and retain the
original integration snapshot. Preflight refreshes ownership snapshots on resume
so resources created by a partial apply or imported during recovery are recognized.
After code changes, start a new run; checkpoints still reject changed source.
Completion requires the full selected workflow, health checks and integration
preservation checks. GitHub registration remains separate when never configured.

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
| Bedrock access | Prepare and verify | Prepare and verify required runtime models |
| Terraform applies | `-auto-approve` | **Plan-first with destroy gate** (§5) |
| Image tag | Source SHA | **Source SHA** (`git rev-parse HEAD`) |
| Backend rollout | Mandatory | **Mandatory** — script fails if rollout fails |
| Database migrations | Runs on the deployed release | **Runs alembic** on Ready replicas of the intended release (§6) |
| Admin bootstrap | Seeds first admin | Skipped — admin already exists |
| GitHub App prompt | Shown at end | Not shown — already configured |
| Preflight | Full (~27 checks) | Partial (AWS auth + cluster reachability) |

---

## 3. Preconditions

Update mode fail-fasts before touching anything if the target does not look
like a live deployment:

1. **State bucket exists** (`adp-terraform-state-<account-id>`)
2. **EKS cluster is `ACTIVE`** (`adp-<env>-eks-cluster`)
3. **Installed module scope is recovered** from Terraform state; the
   `adp-gateway` namespace must be reachable when gateway is in scope
4. **Original agent-context configuration is available** when that module is
   in scope, and its cluster and region match the target

Also verify yourself, before running:

- **You are in the right account.** `aws sts get-caller-identity` must resolve
  to the account you intend to upgrade. If you operate multiple linked
  accounts, this is the single most important check — see §9.
- **Clean checkout.** The image tag comes from `git rev-parse HEAD`; a dirty or
  wrong-branch checkout deploys something other than what you think. Deploy
  from `main` or a pinned release tag.
- **Do not commit tfvars rewrites.** The script substitutes account IDs
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
# Full upgrade (installed modules plus the required agent factory):
./platform/scripts/deploy-all.sh --update

# Gateway-only upgrade, no frontend rebuild (fastest meaningful update):
./platform/scripts/deploy-all.sh --update --gateway-only --skip-frontend

# Repeat a gateway upgrade to verify convergence (still rebuilds the image
# and runs the pricing-refresh drain window):
./platform/scripts/deploy-all.sh --update --gateway-only --skip-frontend

# After reviewing a refused plan and confirming every destroy is benign:
./platform/scripts/deploy-all.sh --update --confirm-destructive
```

---

## 5. The Terraform destroy gate

The gate automatically accepts these routine replacements after checking their
before/after values:

- An API Gateway deployment revision using create-before-destroy on the same
  REST API (the API itself is retained).
- The existing usage-tracker S3 permission when the only supported tightening
  adds the current account's `source_account`, retaining its function, principal
  and source bucket.
- The webhook ScaledJob, warm-pool and image-prepull manifest wrappers when
  namespace, cluster and region stay unchanged. ScaledJob replacement orphans
  existing Jobs, and gradual rollout retains running work.
- The webhook worker gateway rollout marker when protected authority stays
  disabled, its script stays unchanged, the cluster matches the environment,
  and only the configuration digest changes. The marker has no destroy
  provisioner.
- The webhook, GitHub App ID/key, marker-signing and GitLab placeholder secret
  versions when Terraform relinquishes version ownership with `forget`, the
  secret containers remain managed with the same identity and KMS key, and
  only recovery-window or tag metadata changes. Setup/rotation retains the
  actual versions and values.
- The agent-factory migration of gateway intake from an inline IAM grant to an
  already attached managed policy with the same or greater scoped permissions.
  The update uses two complete saved-plan passes: the first retains the inline
  grant while attaching the managed policy, and the second retires the inline
  grant. The same gate accepts deletion
  of only the exact retired runner EKS edit association and inline gateway,
  Bedrock logging and security scan grants removed by the automation role
  split. Changed identities, policy documents or scopes still stop for review.

This is an explicit address-and-value policy, not an exemption for every
`null_resource` or every create-before-destroy change. Stateful replacements and
unknown operations still stop for review. Credential/identity protection cannot
be overridden by the destructive flag.

In update mode, every Terraform apply is replaced by a **plan-first gate**
(`terraform_update_apply`):

1. `terraform plan -detailed-exitcode -no-color` runs and the output is saved.
2. **No changes** → apply is skipped (fast no-op).
3. **Protected credential or identity changes** → the script refuses, even
   with `--confirm-destructive`.
4. **Other changes with no unrecognized deletes/replacements** → applied
   automatically, including the narrowly defined routine replacements above.
5. **Other deletes/replacements or removals from state** → the script refuses
   unless explicitly authorized with `--confirm-destructive`.

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

### Severely outdated deployments

There is no separate "allow destroys" mode — `--confirm-destructive` **is**
that mode. What changes on a very stale deployment is how risky it becomes:

- **Destroys are discovered one module at a time.** The run stops at the
  *first* refusing module; you cannot see what later modules plan to destroy
  without either applying the earlier ones or pre-approving everything. And
  because the flag is global, confirming module 1's benign churn also
  pre-approves — sight unseen — whatever modules 2..N plan to destroy.
  On a stale deployment, that global pre-approval is most dangerous exactly
  when it is most tempting. Workaround today: run the module plans manually
  first (`terraform plan -no-color` per module directory) to build the full
  destroy picture before deciding. Issue #3733 tracks the proper fix
  (`--plan-only` consolidated preview + per-module
  `--confirm-destructive=<modules>` scoping).

- **"Genuinely outdated" does not make stateful destroys OK.** An old
  deployment's RDS still holds real data; Neptune drift still plans destroys
  that lose graphs. If the deployment is so far behind that Terraform wants
  to replace **stateful** resources wholesale, the honest operation is not
  update-with-destroys — it is a deliberate **migrate-or-teardown decision**
  (`undeploy.sh` / `--destroy` + fresh deploy, with data migration planned
  explicitly). The confirm flag is for *reviewed, benign churn*; treating it
  as "the deployment is old, just force it" is how the pre-gate blind
  `-auto-approve` incidents happened.

### Known gate limitations

- The gate covers the Terraform applies inside `deploy-all.sh` and the delegated
  webhook-ingress upgrade. The standalone equivalent is:

  ```bash
  modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh --update
  ```

  Both inspect the saved plan's JSON actions and gate deletes, replacements
  in either action order, and removal from state using the policy above.
  Failed plans or unreadable plan JSON also stop the upgrade. The same saved
  plan is passed to apply.

- History note: the gate's destroy detection was broken (ANSI color codes
  defeated the grep) from its introduction until #3664 (2026-07-11). Runs
  before that date silently bypassed the gate. Regression-tested since
  (`platform/scripts/tests/test-destroy-gate.sh`).

---

## 6. What happens to images, rollouts, and migrations

**Images.** Update mode tags images with the source SHA
(`IMAGE_TAG=$(git rev-parse HEAD)`) for `adp-gateway`,
`adp-agent-gateway`, `adp-chat-agent`, and `adp-agent-runtime`, and forwards that tag to
CodeBuild. This guarantees Kubernetes sees a new image reference and actually
rolls out — the classic `:latest`-push-no-rollout silent failure cannot happen.
The script rebuilds images on repeat upgrades and forces a rollout restart
when the deployment already references the intended tag.

**Rollouts.** `kubectl rollout status` is mandatory — no `|| true`. A failed
rollout fails the run. Before the upgrade reports success, the CDN's
`/api/health` response must contain `{"status":"healthy"}`.

**Migrations and pricing.** Before gateway infrastructure changes, the script
pauses pricing refresh and lets existing invocations drain. After rollout,
`pricing-rollout.py migrate` requires Ready replicas of the exact intended
image, runs `alembic upgrade head`, verifies the revision, and activates the
pricing snapshot. Its `finalize` step verifies pricing refresh and re-enables
the schedule. Failure stops the upgrade with refresh still disabled.

**Frontend.** Rebuilt (`npm ci --include=dev && npm run build` with the correct
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
git rev-parse HEAD               # must match the tag above

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

**Idempotency check (optional after a big update):** re-run the same `--update`
command. Expect no schema changes, healthy rollouts, and a clean final gateway
plan. Expired bootstrap Jobs and deployment artifacts can be recreated during
the run; unexpected stateful changes still require investigation.

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

## 9. Hosted cross-account upgrades are unavailable

Do not use a dashboard-linked AWS role, `/aws-label`, or a `customer_account`
workflow input for an upgrade. Those roles are steady-state inspection or
Bedrock-routing credentials, and the deployment config loader rejects them as a
target before any plan, apply, or destroy step can run.

Run upgrades through the self-managed procedure in this guide with temporary,
customer-controlled bootstrap credentials. The published
`aws_role_deploy_v1.yaml` file is an inspectable foundation-only permission
contract for a future deploy tier; it denies IAM creation and publishing it does
not enable hosted execution or a full installation.

---

## 10. What update mode does NOT cover

- **The ADP platform account** — CI pipelines own it; never run `--update`
  against it.
- **Fresh webhook deploys** — use standalone `--update` or the parent upgrade
  command to enable the saved-plan gate.
- **A consolidated Terraform `--plan-only` preview** — the launcher
  `--dry-run` checks release selection and identity, not Terraform changes. The
  plan-gate output during an actual upgrade shows each module plan. Tracked with per-module
  `--confirm-destructive` scoping in issue #3733.
- **Alembic downgrades** — rollback relies on additive migrations (§8).
- **GitHub App changes** — App registration/installation is UI-driven and
  independent of code upgrades.

---

## Related docs

- [Deployment quickstart](deploy-quickstart.md) — install, select a release and upgrade
- [`deploy-all-update-mode-design.md`](./deploy-all-update-mode-design.md) — full design rationale (#3414)
- [`deployment-manifest.md`](./deployment-manifest.md) — per-resource validation commands
- [`adp-managed-deploy.md`](./adp-managed-deploy.md) — hosted-track status and security contract
- Issues: #3528 (implementation), #3565 (first live run), #3664/#3665/#3666 (fixes from that run), #3543 (open webhook-ingress gate gap)
