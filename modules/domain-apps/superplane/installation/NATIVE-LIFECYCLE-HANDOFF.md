# Governed workspace lifecycle: operator handoff (#5535)

This is a code-review and release handoff, **not** deployment authorization or
evidence of an installed lifecycle. The API now checks a current, authenticated,
non-consuming installed-worker binding before admitting native workspace lifecycle
work; the registered worker handles approved phases through the durable operation
queue. An unavailable or mismatched binding, authority or dependency refuses
admission before writes. Tests use disposable databases and transport doubles.

## Release decision and private inputs

Foreground review/merge and the Wave 6 operations evaluator (#5540) own release
authorization and live evidence. Before any account or cluster operation, the
operator must obtain the retained installation gate's approval for the exact
target, scoped role, immutable source/release image digests, expected spend,
database migration/backup and restore owners, and rollback owner. Confirm the
selected identity, cluster and account against private target configuration;
never derive them from this public document. Use a controlled code-only merge
where automated pushes otherwise deploy. Keep secrets, environment files, plans,
receipts and verification tokens in an access-controlled location outside Git.

The [installation contract](README.md) specifies the prerequisites, including
Gateway transport version 2, domain-only Terraform state/locks, scoped database
roles and a matching snapshot. The control-plane-only environment template can
install management with zero workspaces, but `control_plane_ready` does **not**
mean `workspace_execution_ready`. The current paid-worker installer still reports
`source-preparation-only`, `activation_available: false` and refuses selection
before external tools. Its image publication/entrypoint and live identity, schema,
network, registry and binding gates are not satisfied by this PR. Do not enable
paid lifecycle admission or claim installed binding until a separately reviewed
release resolves this gate and its actual dependency checks pass. Do not turn to
the legacy executor as a fallback. Inventory and safely quiesce previously
admitted operations before a mode switch; changing flags does not cancel work.

## Reviewable and authorized installer sequence

These commands are templates, **not commands run by this developer**. Supply
complete private files (`/secure/environment.yaml` and `/secure/release.yaml`)
from the authorized operator store; `/secure/superplane-run` must be private and
reused only with `--resume`. The default plan does not contact AWS:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run
```

Only after the target and release are authorized, preflight checks scoped cloud
and cluster identities, image/source provenance, Gateway support, database and
backup, and produces a checked domain Terraform plan. Review the saved plan and
its `plan_sha256`. For the management-only stage use a privately completed
`control_plane_only: true` environment with no workspace fields. Do not run a
full paid-worker install while the activation gate above remains closed.

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --preflight
```

**Only with explicit authorization for the exact reviewed plan, database writes,
route publication and recovery owner**, supply `SUPERPLANE_VERIFICATION_TOKEN`
privately in the process environment (not in argv or Git), then execute:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --execute \
  --approved-plan-sha256 <reviewed-plan-sha256>
```

Preflight repeats and refuses plan drift. Record the private `receipt.json` with
run/source/release/environment/plan identity, migration head and Job, pinned
Secret versions, owned object UIDs, authenticated public and private checks and
the route observation. A successful installer reports `installed-and-verified`;
management-only additionally records `control_plane_ready: true`,
`registered_workspaces: 0`, `workspace_execution_ready: false` and successful
post-restart reads. A ready pod or applied manifest is not this evidence.

For a separately approved **full lifecycle** release, the evaluator must also
confirm the exact installed worker's registry, queue, role, ServiceAccount,
namespace, image and schema against the authenticated Gateway binding proof;
verify the authorized organization's `/internal/installation?org_id=<org-uuid>`
readiness reports `paid_worker_binding.executable` and `paid_admission_enabled`
true with current authority and dependency capabilities. The Gateway proof is
an internal authenticated POST, not a public URL or a substitute for the API's
readiness gate. From an authorized private network, the evaluator can use these
read-only requests. Set `SUPERPLANE_PRIVATE_API_URL`, `GATEWAY_PRIVATE_URL` and
`ORG_UUID` from the private target record. Each restricted `curl` config file
must supply the respective authentication header; never print or commit these
files. `jq` checks the admission flags; inspect and retain the remaining proof
fields only in the private evidence store:

```sh
curl --fail-with-body --silent --show-error \
  --config /secure/superplane-read.curl \
  --get --data-urlencode "org_id=${ORG_UUID}" \
  "${SUPERPLANE_PRIVATE_API_URL}/internal/installation" \
  | jq -e '.paid_admission_enabled == true and .paid_worker_binding.executable == true and (.dependencies | all(.[]; . == true))'
```

```sh
curl --fail-with-body --silent --show-error \
  --config /secure/gateway-internal.curl \
  --header 'Content-Type: application/json' \
  --data "{\"domain\":\"superplane\",\"org_id\":\"${ORG_UUID}\"}" \
  "${GATEWAY_PRIVATE_URL}/internal/v1/controller-execution/binding-proof" \
  | jq -e '.installed == true and .domain == "superplane" and .worker_registry_id != null'
```

Test one authorized create and delete through the ordinary API,
then inspect operation IDs, outbox/attempt/fence and worker receipts, provider
outcomes and journal continuity. Separately test stale/mismatched identity,
lost authority, unapproved teardown and ambiguous/partial results: no pre-write
mutation on refusal and no false completion on uncertain execution. Record
observed cleanup and cost exposure; never infer zero spend from a test fixture.

## Recovery and rollback (separate authorization)

For interruption, stop and confirm the prior installer and child processes,
inspect the exact migration Job and Terraform lock and use the same receipt:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --recover-lock \
  --confirm-stopped <recorded-run-id>
```

Never steal a `recovery-required` lock by timeout. Re-run preflight and obtain
approval for any changed plan. An image/configuration rollback needs a **prior
successful receipt from the same environment**, the matching compatible schema,
the private verification token and a *new* private output directory:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-rollback --rollback /secure/previous/receipt.json
```

The installer fences/disables the current route, restores pinned secret versions
and owned service images, then rechecks private/public service and unrelated ADP
health. It does **not** downgrade schema; incompatible migrations require the
named database restore owner's decision. If public route state or ownership is
uncertain, retain its lock and receipt for reconciliation rather than assuming
rollback succeeded. Disabling the feature gate prevents new public access but
does not cancel queued or running operations. Preserve operation stores,
attempts, outbox, journals, database/backups and external provider resources;
verify outstanding obligations before any further cleanup. The installer's
`--cleanup` is not a provider-resource reclamation command. #5540 owns the
actual rollback drill and records its private before/after receipts and denial,
recovery, residual-resource and cost evidence.
