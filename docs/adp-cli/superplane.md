# Superplane commands

[CLI guide](README.md) · [Full command reference](command-reference.md#superplane)

The `adp superplane` command group uses your ADP login for workspaces, GPU
workloads, model deployments, cloud-account registration and provider credentials.

## Availability

The CLI command group is included in the checked source. Its operational
requests use the gateway's `/api/superplane/v1` API contract; the corresponding
server integration must be deployed before these workflows work. CLI help and
local workspace selection alone do not establish that the service is available.
The current helper also reports unresolved AWS onboarding prerequisites, so
account registration must not be treated as complete infrastructure provisioning.

Use these examples only with an enabled, compatible Superplane service.
Mutating commands submit requests directly: this group has no `--dry-run` or
`--yes` option. The backend enforces permissions and quotas.

## Choose a workspace

```bash
adp superplane workspace list
adp superplane workspace use research
adp superplane workspace describe
adp superplane node
adp superplane quota show
adp superplane cost
adp superplane events --limit 20
```

`workspace use` saves a local default. `--workspace NAME` overrides it for an
operational command, for example `adp superplane cost --workspace research`.
It does not change which ADP environment you are signed in to.

To create a workspace, choose its isolation and optional budget limits:

```bash
adp superplane workspace create --name research --isolation research \
  --account research-aws --budget-daily 25 --budget-gpus 1
```

Supported isolation values are `dedicated` (default), `namespace` and
`research`. Research isolation requires `--account`.

Kubernetes access information is returned by
`adp superplane workspace kubeconfig --workspace research`. With `--json`, the
kubeconfig is in `detail.kubeconfig`; stdout is a JSON envelope, not a raw
kubeconfig file. Treat generated access configuration as private.

## Model deployments and quotas

Replace `MODEL_ID` with a model supported by your service:

```bash
adp superplane deploy create --workspace research --name demo \
  --model MODEL_ID --precision bf16
adp superplane deploy list --workspace research
adp superplane quota set --workspace research --max-gpus 1 \
  --max-nodes 1 --max-cost-per-day 25 --allowed-clouds aws
```

Supported precision values are `fp16` (default), `bf16` and `fp8`. To request
deletion of that deployment:

```bash
adp superplane deploy delete --workspace research --name demo
```

Stopping the local CLI does not cancel work already accepted by the service or
prove provider resources stopped billing. Check service events and resource
state after an interruption.

## Cloud accounts

For an AWS account, first establish an ADP AWS connection, then use its ID:

```bash
adp aws list
adp superplane aws-onboard register --account-id 123456789012 \
  --credential-id ADP_CONNECTION_ID --name research-aws
adp superplane account list
```

This registers an existing connection with Superplane. It does not create all
the EKS, IAM or ingest resources needed by a workload. Read the command's
reported prerequisites; a personal read-only ADP role is not evidence of full
Superplane provisioning permissions.

Other providers use `adp superplane account onboard --name NAME --provider
PROVIDER [--account-id ID]`. `adp superplane account delete ACCOUNT_ID` requests
account deregistration from the service.

## Provider credentials

```bash
adp superplane provider add --name research-provider --provider nebius --type api_key
adp superplane provider list
```

The credential is entered at a hidden prompt and stored in ADP's vault.
Automation can pipe the value to `provider add … --stdin`; do not supply
`--api-key`, `--token`, `--secret` or `--password` arguments. Supported types are
`api_key`, `oauth_token`, `bearer`, `basic_auth` and `config_file`.

`adp superplane provider delete CREDENTIAL_ID` removes the service registration
and vault credential. Read any partial-cleanup error before assuming both were
removed.

Organization and user administration belongs to ADP settings.
`adp superplane org` and `adp superplane user` print those destinations; they do
not create organizations or users.
