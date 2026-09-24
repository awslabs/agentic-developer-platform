# Superplane commands

[CLI guide](README.md) · [Full command reference](command-reference.md#superplane)

The `adp superplane` command group uses your ADP login for workspaces, GPU
workloads, model deployments, cloud-account registration and provider credentials.

## Availability

The CLI command group is included in the checked source. Its operational
requests use the gateway's `/api/superplane/v1` API contract; the corresponding
server integration must be deployed before these workflows work. CLI help and
local workspace selection alone do not establish that the service is available.

Every command's method, path and body is checked against the gateway's forwarding
allowlist and the service's own request models by
`modules/gateway/tests/cli/test_superplane_contract.py`. That establishes the
requests are the ones the server accepts; it does not establish that a given
deployment has the service enabled.

Use these examples only with an enabled, compatible Superplane service.
Remote mutations support `--dry-run`, which resolves their target and prints the
request plan without writing. Applying a mutation requires an interactive `yes`
confirmation; automation must pass `--yes`. This never bypasses backend
permissions or supplies missing inputs.

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

`workspace use` saves a local default. `--workspace` overrides it for an
operational command, for example `adp superplane cost --workspace research`.
It does not change which ADP environment you are signed in to.

`--workspace` accepts the name you see in `workspace list` or the workspace id
directly. Names are resolved to the id the API requires, which needs permission
to list workspaces; passing an id skips that lookup. A name matching more than
one workspace is reported with the candidate ids and nothing is changed — choose
one and pass its id.

To create a workspace, choose its isolation and optional budget limits:

```bash
adp superplane workspace create --name research --isolation research \
  --account research-aws --budget-daily 25 --budget-gpus 1
```

Supported isolation values are `dedicated` (default), `namespace` and
`research`. Research isolation requires `--account`.

Mutations and recovery require an access token with a nonblank tenant claim.
Org-less password sessions are refused before mutation and existing receipts
are retained. Supporting these sessions remains an unresolved production
authentication integration; neither metadata lookup nor a guessed organization
can bind such a token safely. Read commands retain their existing server checks.
Each command keeps one access token in memory for its requests and receipt
identity, and receipts name the gateway fixed by that command's transport.
A login, organization switch or gateway configuration change in another terminal
cannot retarget that command's recovery context; if its token expires, the request
fails and must be retried.

Workspace creation generates an operation ID; deployment creation requires the
explicit `--operation-id` used during preview. Both save that ID in private CLI
state before the create POST. Identical invocations in the same
signed-in deployment and tenant reuse that operation ID, including concurrent
invocations and retries after the server completes the operation.
If delivery times out, disconnects, returns a 5xx, or returns a malformed success,
the CLI reports the operation ID; rerun the identical command to reconcile it.
Changing workspace inputs starts a different operation and retains the earlier
receipt. Deployment inputs are bound to the reviewed operation ID: changing them
requires a new preview and approval, and cannot reuse that ID. Successful creates retain the resource ID and receipt because a server
success does not prove the result reached your terminal. An identical create
therefore reconciles the original resource. Failed, deleting or deleted resources
retain their receipts and block further identical creates. Inspect that resource
before intentionally choosing a different name for a new create; there is no
automatic receipt reset. A domain version that cannot confirm this replay
contract is reported as unavailable before either create request is sent.
If the domain has accepted the operation but still reports `Provisioning`,
`Pending`, `Running` or `Unknown`, the CLI reports `pending` (exit 4) and names
the read command that reconciles the same resource. It does not render an
in-progress operation as completed.

Kubernetes access information is returned by
`adp superplane workspace kubeconfig --workspace research`. With `--json`, the
kubeconfig is in `detail.kubeconfig` and its expiry in `detail.expires_at`;
stdout is a JSON envelope, not a raw kubeconfig file. Treat generated access
configuration as private, and re-run the command after the reported expiry
rather than assuming the credential keeps working.

## Cost

```bash
adp superplane cost --workspace research --start-date 2026-09-01 --end-date 2026-09-30
adp superplane cost --org
```

Workspace cost and organization-wide cost are separate queries: pass
`--workspace` (or rely on the selected workspace) for one workspace, or `--org`
for the whole organization. They cannot be combined. `--start-date` and
`--end-date` take ISO 8601 dates and are optional; the service chooses the
window when they are omitted.

## Audit events

```bash
adp superplane events --limit 20
adp superplane events --resource-type deployment --action created \
  --start-time 2026-09-01T00:00:00Z
```

Events are filtered by `--resource-type`, `--user`, `--action`, `--event-type`,
`--start-time` and `--end-time`, with `--limit` (1-500, default 50) and
`--offset` for paging.

**There is no workspace filter.** `events --workspace` was accepted by an earlier
build and then ignored by the service, so it listed every workspace's events
while appearing to be scoped to one. It is now refused rather than silently
unscoped; use `--resource-type workspace` or `--resource-type deployment` with a
time range instead. If a script relies on the old flag, its previous output was
not workspace-scoped.

## Model deployments and quotas

Choose one request UUID and retain it from preview through submission and recovery.
Replace `PROFILE_ID` with a configured serving profile and `MODEL_ID` with its
supported model. The profile supplies the reviewed image and authentication.

```bash
adp superplane deploy preview --workspace research --name demo \
  --model MODEL_ID --precision bf16 --profile-id PROFILE_ID --operation-id REQUEST_UUID
# Review the returned controller_plan, allocation_id and revision, then request approval.
adp superplane deploy preview --workspace research --name demo \
  --model MODEL_ID --precision bf16 --profile-id PROFILE_ID --operation-id REQUEST_UUID \
  --request-approval --plan-revision REVIEWED_REVISION --yes
adp superplane onboarding approval show --approval-id APPROVAL_ID
# A selected human approver uses their own ADP session for this decision.
adp superplane onboarding approval decide --approval-id APPROVAL_ID --result allowed-once --yes
adp superplane deploy create --workspace research --name demo \
  --model MODEL_ID --precision bf16 --profile-id PROFILE_ID --operation-id REQUEST_UUID \
  --approval-id APPROVAL_ID --plan-revision REVIEWED_REVISION --yes
adp superplane deploy list --workspace research
adp superplane quota set --workspace research --max-gpus 1 \
  --max-nodes 1 --max-cost-per-day 25 --allowed-clouds aws
```

Preview returns the exact approval request. `--request-approval` sends that request
unchanged after checking the reviewed revision; it does not record an approval
decision. The server enforces eligible human approval separately. `--yes` confirms
the CLI submission and cannot substitute for that approval. Creation repeats the
same preview inputs with the approval ID and revision.

`--name` is required and uses lowercase letters, digits and hyphens.
Deployment listing preserves the durable deployment ID, operation ID/state and
provider UID so accepted or uncertain work can be reconciled.

Supported precision values are `fp16` (default), `bf16`, `fp8`, `awq` and `int8`.
`--serving-framework vllm|sglang`, `--replicas`, `--gpu-per-replica`,
`--tensor-parallel-size` and `--max-model-len` are optional; each
omitted option takes the service's own default rather than one chosen locally.
The current controller recipe accepts one serving replica; unsupported profile or
replica combinations are refused during preview.
The service uses the workspace's recorded namespace. Missing namespace ownership
requires reconciliation before deployment.

Teardown uses the deployment UUID returned by create or list and a separate request
UUID. Its preview retains the original allocation. Review and obtain human approval
before submitting the same teardown identity:

```bash
adp superplane deploy teardown-preview --workspace research --id DEPLOYMENT_UUID \
  --operation-id TEARDOWN_REQUEST_UUID
adp superplane deploy teardown-preview --workspace research --id DEPLOYMENT_UUID \
  --operation-id TEARDOWN_REQUEST_UUID --request-approval \
  --plan-revision TEARDOWN_REVISION --yes
# The selected human approver decides TEARDOWN_APPROVAL_ID through the approval commands above.
adp superplane deploy delete --workspace research --id DEPLOYMENT_UUID \
  --operation-id TEARDOWN_REQUEST_UUID --approval-id TEARDOWN_APPROVAL_ID \
  --plan-revision TEARDOWN_REVISION --yes
```

`--dry-run` on preview, approval issuance, create or delete sends no mutation.
After a lost reply, retain every original input and operation ID when retrying.
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

The connection must belong to the signed-in human user, be in the selected
tenant, have a successful server-recorded AWS verification, and match
`--account-id`. A connection verified before this provenance check was deployed
must be verified once again before its first Superplane registration. The CLI
sends only its opaque ADP credential ID. The gateway resolves the stored role ARN
and ExternalId server-side, and the Superplane domain performs its normal
`provision` authorization before creating authoritative account state. Neither
trust value is printed or accepted as a command-line argument. The gateway
validates the resolved values against the domain contract before forwarding and
removes them from successful and failed downstream responses, including
validation errors.

Use `--dry-run` to inspect the reference-only request without resolving the
connection or writing. Retrying the same registration returns the same domain
record; changing the connection metadata for an already registered account is a
conflict rather than a second registration.

`adp superplane account list` and `adp superplane account delete` work normally.
Delete accepts the registration's record id, or the cloud account ID or name you
registered it under, which is resolved to that record id; an ambiguous value is
reported with the candidates and nothing is deregistered.

## Provider credentials

```bash
adp superplane provider add --name research-provider --provider nebius --type api_key
adp superplane provider list
```

The credential is entered at a hidden prompt and stored in ADP's vault.
Automation can pipe the value to `provider add … --stdin`; do not supply
`--api-key`, `--token`, `--secret` or `--password` arguments. Supported types are
`api_key`, `oauth_token`, `bearer`, `basic_auth` and `config_file`.

Provider mutations require a signed-in session. Recovery receipts are bound to
the selected deployment, gateway, stable principal, and the token's organization
claim. A token without that claim cannot authorize a Superplane mutation or
recovery; support for org-less login remains an unresolved authentication
integration. Changing any bound value refuses recovery without a resource request
and retains the receipt.
Receipts created by an older, unbound CLI are also retained for manual
reconciliation rather than guessed into the current context.

Adding a provider writes to two places: the secret goes to ADP's vault through
an idempotent `PUT /auth/credentials/{operation-uuid}`, and only that opaque id
is registered with the service as metadata. The CLI persists the non-secret
operation UUID before sending the value. Each credential has **two identifiers**
— the service's own record id, and the ADP credential id that record references.
`provider list` shows both.

If registration is definitively rejected, the command removes only the exact
credential operation it created. A timeout, disconnect, 5xx, or malformed success
response is different: either write may have committed, so the CLI keeps a private
recovery receipt rather than deleting by label or inventing a new identity.
Reconcile it with `provider add --recover ADP_CREDENTIAL_ID --yes`. If the vault
confirms the credential and no PUT conflict was recorded, no secret is requested.
If metadata is absent, add `--stdin` and supply the same secret; the retry reuses
the same UUID. Missing metadata does not prove the stored secret is absent.
A PUT conflict retains the receipt and requires `--recover ADP_CREDENTIAL_ID
--stdin --yes` with the original secret even if metadata becomes visible.
Later errors, including 404 or 405 during recovery, retain the original receipt.

`adp superplane provider delete CREDENTIAL` accepts either identifier and removes
the service registration and then the vault credential. Read any partial-cleanup
error before assuming both were removed. A value matching no registration is
refused without deleting anything, rather than guessed at as a vault id.

Organization and user administration belongs to ADP settings.
`adp superplane org` and `adp superplane user` print those destinations; they do
not create organizations or users.

## Saved workspace lifecycle plans

The onboarding CLI can review a saved phase and request its exact server-provided
approval through the composed lifecycle API routes. Authorization, current policy
and exact approval still govern each submission. An unavailable command exits 4.

```bash
adp superplane onboarding lifecycle list --workspace WORKSPACE_ID
adp superplane onboarding lifecycle plan --workspace WORKSPACE_ID --artifact-id ARTIFACT_ID
adp superplane onboarding lifecycle request-approval --workspace WORKSPACE_ID \
  --artifact-id ARTIFACT_ID --plan-revision REVIEWED_REVISION --yes
adp superplane onboarding approval show --approval-id APPROVAL_ID
# A selected approver uses their own ADP session to decide the request.
adp superplane onboarding approval decide --approval-id APPROVAL_ID --result allowed-once --yes
adp superplane onboarding lifecycle continue --workspace WORKSPACE_ID \
  --artifact-id ARTIFACT_ID --plan-revision REVIEWED_REVISION --yes
```

Review the account, region, resource changes, estimate and saved plan hashes
before requesting approval. The CLI preserves one request identity across plan
review, approval and continuation. It checks the current approval and rereads
the plan immediately before submission; changed hashes or an expired, revoked,
rejected or unrelated approval prevent continuation. `--dry-run` on
`request-approval` and `continue` writes no receipt and sends no domain request.

If a continuation response is lost, retain its request reference and use
`adp superplane onboarding operation recover --key REQUEST_ID`. Repeating
`lifecycle continue` recovers the submitted receipt, including after the API has
advanced past its source proposal. A completed phase does not establish workspace
readiness; check `adp superplane onboarding readiness --workspace WORKSPACE_ID`.
