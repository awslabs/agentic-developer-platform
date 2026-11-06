# Adapter foundation bootstrap

The normal installer uses `installer.py` to coordinate publication permissions
and the Terraform foundations. It calls `bootstrap.sh`'s Python driver to install the publication and EKS
access foundations together. It uses the confirmed operator AWS credentials;
the protected runtime publisher does not receive these provisioning permissions.
The driver requires Python 3, AWS CLI, and Terraform 1.10 or newer (below 2.0).

The installer resolves an existing private, versioned state/archive bucket,
active EKS cluster and its OIDC provider, and the registered environment
connection role. That role must already have a `STANDARD` EKS access entry for
`adp:runtime-verifier-<environment>`; Terraform validates it before provisioning.
The installer enables adapter publication on the existing tools deployment role
in its original automation state. Network policy remains a separate runtime
integration. GitHub App creation and registration are unchanged.

## Use the platform installer

Set `ADP_TOOLS_ADAPTERS_CONFIG` to a private JSON file with two fields:

| Field | Contents | How it is used |
|---|---|---|
| `foundation` | The `backend_bucket` and `variables` object described below | Plans the adapter namespace, access, repository and build project in the adapter state |
| `automation.backend` | Existing automation backend's `bucket`, `key`, `region`, `encrypt: true`, and `dynamodb_table` or `use_lockfile: true` | Reuses the original locked state; verifies that it already owns the tools publisher |
| `automation.variables` | Existing reviewed automation inputs, including `name_prefix`, `environment`, `aws_region`, `cluster_name`, `repository`, and `enable_tools_deployment: true` | Retains every supplied input and enables only `enable_tools_adapters_publication` |

The backend bucket must match the foundation backend. The current publisher
policy uses the private, versioned `adp-terraform-state-<account>` archive bucket
with SSE-S3. A different archive bucket or SSE-KMS key requires reviewed publisher
permissions first. Retain optional gateway-key and private-network inputs;
omitting them must not remove previously granted authority.

```bash
export ADP_TOOLS_ADAPTERS_CONFIG=/private/adapters-installer.json
# Existing environment: provision only the adapter prerequisites.
bash platform/scripts/deploy-all.sh --tools-adapters-only --env dev
# Plan both states without applying infrastructure:
bash platform/scripts/deploy-all.sh --tools-adapters-only --env dev --ci
```

Full platform installs and updates run the same step after the agent factory
when this configuration is supplied. Other component-only invocations retain
their scope. The configuration's account, region and environment must match the
launcher's target, and its content is bound into full-deployment checkpoints.
The tools publisher and registered verifier must already exist with their
correct EKS groups. The installer neither creates an alternative publisher nor
modifies their trust policies.

Both plans must pass before either apply starts. The automation comparison
renders the current source with adapter publication disabled and enabled; a
changed existing policy must match the disabled form before it can transition
to the enabled form. Only the two adapter policy resources and the three
existing policy documents can change. Unrelated changes, deletion, replacement,
imports and ownership changes are refused. The copied source, configuration
and binary plans are checked again before applying. After the foundation, IAM
policy documents and the publication attachment are read back from AWS.

The command prints the private plan/receipt directory. An interrupted installation
can be run again: it creates new plans against the same remote states and retains
the earlier evidence. It never clears state or retries a stale binary plan.
The installer reports `runtime_verified: false`; the protected workflow and
independent runtime evaluation are still required.

For a separately reviewed saved plan, run from the repository root:

```bash
python3 -m modules.tools.adapters.installer plan \
  --config /private/adapters-installer.json --work-dir /private/adapter-install-001 \
  --account "$ADP_ACCOUNT_ID" --region "$ADP_REGION" --environment "$ADP_ENVIRONMENT"
# Use the same arguments with `apply` after reviewing the saved plans.
```

## Foundation driver

Write a private JSON configuration with exactly `backend_bucket` and `variables`.
`variables` contains all [deployment root inputs](deployment-infra/README.md),
including `runtime_verifier_role_arn`; `source_kms_key_arn` may be omitted for
SSE-S3. No credentials belong in this file. The backend key is always
`tools/adapters/<environment>.tfstate`, with encryption, S3 locking, and the
confirmed account restriction. Dev and prod never share a key.

```bash
bash modules/tools/adapters/bootstrap.sh plan \
  --config /private/adapter-foundation.json --work-dir /private/adapter-plan-001
# Review plan.json and manifest.json, then apply the same saved binary plan.
bash modules/tools/adapters/bootstrap.sh apply \
  --config /private/adapter-foundation.json --work-dir /private/adapter-plan-001
```

The plan directory must initially be empty. Keep it in the deployment journal,
outside the checkout, for review and recovery. It contains private command logs,
the explicit backend and variables, an exact snapshot of the three Terraform
modules, the binary plan, and a manifest binding them. The driver isolates
Terraform data and CLI configuration and clears ambient Terraform variable/CLI
overrides and Kubernetes configuration. Identity and bucket checks precede init.
The same account, configuration, sources and binary plan are checked again on
apply. Deletions, replacements and implicit imports/address moves are refused.

The driver does not prompt; the authorized installer can review the generated
plan and invoke apply. If a plan is stale or an apply was interrupted, reconcile
the remote state and create a new plan directory against that same backend.
Never remove state, import resources silently, or switch to an empty backend to
avoid a conflict. Retain any existing independent child states until a reviewed
state migration transfers their resources into this composed root.

`foundation_applied` means Terraform completed and its target outputs match.
It does not establish a deployed adapter. The existing tools deployment workflow
must still publish a qualified immutable image, apply the workload, verify live
runtime and signed evidence, and pass independent evaluation before acceptance.
