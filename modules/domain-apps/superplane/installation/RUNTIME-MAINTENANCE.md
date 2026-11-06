# Paused runtime image maintenance

`python -m installation.runtime_maintenance` updates four existing domain objects
without replaying installation, Terraform or migration. Use an exact clean merged
source checkout, the selected installation operator, current environment document
and independently qualified resolved release lock. Reconstructed current settings
are inputs, not historical verification. The command creates a new receipt.

The closed change consists of the API primary image and existing source/release
environment values; the paused ScaledJob's `paid-worker` image; only the image
digest within `superplane-paid-worker-binding`'s `binding.json`; and
`SUPERPLANE_DOMAIN_SCHEMA_HEAD` within `superplane-paid-worker-config`. All other
configurable fields, identities, state, TLS references and launch fences remain.

For an API update that retains an independently sourced worker, add
`--retain-worker` when creating the plan. Keep the worker's actual build revision
in the release lock. The selected immutable worker image must already be installed,
its binding must match the selected installation, and its maintained configuration
values (including schema head) must match the reviewed environment. This mode
preserves the worker image, exact binding bytes and all worker configuration. The
normal installation and maintenance defaults still require the worker's source to
match the release. API build-context and clean-checkout verification remain in force.

Optionally include `--worker-active-deadline-seconds` when creating the same plan.
The value must equal the reviewed environment's `paid_worker.active_deadline_seconds`
and satisfy its mode-specific bounds. The maintained worker projection supplies the
value; only the existing ScaledJob `spec.jobTargetRef.activeDeadlineSeconds` field
changes. Without this option the current deadline is preserved. Both selections
are bound into the saved plan and its digest; apply, observation and lock recovery
use those saved selections without repeating the plan-only flags. Existing worker
Jobs must already be terminal; the option does not alter a running Job's deadline.

First complete any required real schema migration through its existing owner.
The maintained `Installer.migrate()` primitive runs only its exact run-ID Job
under the native installation lock, using the scoped migration Secret and selected
image/head. It does not require or imply a whole installation or bootstrap run.
Its finite pod retains NetworkPolicy labels but remains explicitly unready for
the API Service; Job completion still follows the main process's terminal exit.
Record actual migration observations in a new schema-maintenance receipt. The
runtime maintenance plan and apply use the existing read-only
`app.installation database` command inside an owned API pod to require the actual
target database/schema/head. A changed release lock or worker setting cannot
substitute for that migration. The schema is checked again after runtime rollout.

The recovery transport image is preserved by default. To replace it with the same
selected API image, first independently qualify that image's transport routes and
existing TLS/CA behavior. Pass `--transport-mode matched-api` and the actual private
evidence digest through `--transport-qualification-sha256`. This is an operator
evidence reference; the command does not perform or replace that qualification.

From the Superplane module directory, prepare a read-only saved plan:

```bash
python -m installation.runtime_maintenance \
  --environment "$MAINTENANCE_ENVIRONMENT" \
  --release-lock "$MAINTENANCE_RELEASE_LOCK" \
  --output "$MAINTENANCE_OUTPUT" --plan
```

Review private `maintenance-plan.json` and its reported SHA-256. Before/after
objects bind their UID, installation owner and every configurable field, excluding
only Kubernetes bookkeeping/status. Do not publish this private configuration.
The Deployment controller's root `deployment.kubernetes.io/revision` annotation
is bookkeeping; every pod annotation and other configurable annotation is retained.
Use a new output directory; an existing plan is never overwritten. Planning checks
account/operator/cluster/source, disabled dispatch including actual API pods,
paused KEDA and terminal worker Jobs/pods. Exact ECR manifest bytes bind requested
image digests to their unique linux/amd64 platform digests. No cluster mutation or
installation lock occurs during planning.

After authorization of the exact saved plan, apply it:

```bash
python -m installation.runtime_maintenance \
  --environment "$MAINTENANCE_ENVIRONMENT" \
  --release-lock "$MAINTENANCE_RELEASE_LOCK" \
  --output "$MAINTENANCE_OUTPUT" --apply \
  --approved-plan-sha256 "$MAINTENANCE_PLAN_SHA256"
```

Application takes the native conditional S3 Installer lock. Before every write it
rechecks all four objects and quiescence. JSON Patch tests the live UID,
resourceVersion, owner and replaced values without SSA force. The resourceVersion
is selected after rechecking saved configurable state, allowing server status
bookkeeping without allowing configuration drift. Worker/config/binding change before
API rollout. Actual API/transport image IDs must match the requested or verified
platform digest; pod identity follows Deployment and ReplicaSet ownership.
Terminal finite Job pods and explicitly unready probes are excluded; a potentially
serving foreign owner refuses. Dispatch and KEDA remain disabled.

A timeout or lost response retains the native lock and pre-write intent. Use
`--observe` with the same inputs and approved plan digest to inspect objects as
`previous` or `desired`; a third state refuses. Observation neither claims
completion nor releases the lock. After verifying the recorded process and children
have stopped, use `--recover-lock --confirm-stopped "$MAINTENANCE_RUN_ID"` with the
same inputs/digest for ordinary Installer lock recovery. A subsequent `--apply`
observes exact already-applied objects without resending their writes and finishes
only remaining work. Unrelated changes require investigation and a fresh plan.

`runtime-maintenance-complete` establishes only this paused maintenance. It does
not invent migration/bootstrap history or whole-installation readiness. Shared
Gateway/Harness bindings/releases, provider enrollment renewal, authenticated
runtime verification, native recovery and activation remain separate owner gates.
Use the [shared binding owner procedure](../infra/shared-operation-authority/BINDING-MAINTENANCE.md)
for its isolated Terraform saved plan and exact-digest ConfigMap maintenance.
