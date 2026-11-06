# Paused worker binding maintenance

`python -m installation.shared_binding_maintenance` changes only the existing
Superplane binding's `worker_image_digests` in the shared Gateway authority
ConfigMap. The shared webhook Terraform backend remains the resource owner.
The command neither releases Gateway images nor changes its schedule, permissions,
other configuration, registrations or domain launch fences.

Use this after qualified image maintenance has updated the still-paused domain
worker and its local binding. Select a clean merged source checkout, the same
current installation environment and resolved release lock, and the protected
shared Terraform operator. That operator can differ from the app installer.
The current shared input overlay must preserve the installed registration,
provider authority and unrelated shared settings; reconstruction is current input,
not historical installation evidence.

If the maintenance tooling was repaired after the runtime release, select a clean
checkout at the exact merged tooling commit and pass
`--maintenance-source-revision "$MERGED_MAINTENANCE_REVISION"` to every plan,
apply and observe invocation. Keep the original qualified runtime release lock.
Both revisions must belong to `main`, and the tooling revision must descend from
the original runtime revision. Their source diff may contain only
`installation/runtime_maintenance.py`, `installation/shared_binding_maintenance.py`,
their two named `tests/installer_checks/test_*_maintenance.py` files, this guide,
and `installation/RUNTIME-MAINTENANCE.md`. Runtime code, Terraform, release files
and all other files must remain identical. The new receipt binds both revisions
and the exact allowed changed paths; existing release locks and receipts are not
rewritten. Preserve any earlier refused plan and prepare a fresh plan in a new
directory using the fixed tooling.

From the Superplane directory, prepare a native Terraform plan:

```bash
python -m installation.shared_binding_maintenance \
  --environment "$MAINTENANCE_ENVIRONMENT" \
  --release-lock "$MAINTENANCE_RELEASE_LOCK" \
  --inputs "$CURRENT_SHARED_INPUTS" \
  --output "$BINDING_MAINTENANCE_OUTPUT" --plan
```

The command checks the selected account, actual shared operator role identity,
registered producer/worker identities, management-cluster TLS context, installation
ownership and quiescence. The actual paused worker and its local binding must
already contain the selected immutable image. The command records current object
identities, the state lineage/serial and the source revision in a private receipt.
The shared input namespace must equal the installation's explicit Gateway namespace.

The native plan targets `kubernetes_config_map.worker_gateway[0]`. Its dependency
graph can include unrelated resources with independently pending changes. Following
the maintained tick Redis procedure, an exclusive temporary Terraform override
preserves only the existing shared worker boundary's policy/tags and the marker
secret's retention/tags. The override does not change these resources. It refuses
if their source gains lifecycle behavior that the override would replace, and
is removed after planning. The receipt records the exact preservation declaration
and hashes of the relevant source files.
Fresh observations before and after planning/application also bind the boundary's
policy identity, default version, document and tags, and the marker secret's live
identity, encryption, deletion/rotation metadata and tags. The recorded marker
recovery window comes from its Terraform state: AWS exposes a recovery deadline
only after deletion. No secret value is read. Changed observations refuse a claim
that these dependencies were preserved.

Every other managed resource must be a no-op. The guard permits only the selected
worker digest change inside the existing binding, verifies the observed ConfigMap
UID/resourceVersion and all other settings, and refuses unknown configuration,
replacement, import, state moves and drift deletions. Native Terraform refresh and
backend locking remain enabled. Review the private receipt, `binding.tfplan`, and
`desired-inputs.tfvars.json`; the latter must be retained for subsequent shared
Terraform operations. It is not installed as a public or automatic repository
override.

Terraform 1.14 reports `complete: false` for targeted plans because they omit
resources outside the target graph. Such a plan is accepted only with explicit
`applyable: true`, `errored: false`, no deferred changes, and the same resolved
target and closed-change checks above. Resource checks for the selected graph,
including dependencies and their instances, must pass. Unevaluated checks outside
that graph may remain unknown; failed/error checks are refused. This establishes
the bounded binding change, not whole-configuration convergence.

Apply the reviewed saved plan within the existing maintenance authorization:

```bash
python -m installation.shared_binding_maintenance \
  --environment "$MAINTENANCE_ENVIRONMENT" \
  --release-lock "$MAINTENANCE_RELEASE_LOCK" \
  --inputs "$CURRENT_SHARED_INPUTS" \
  --output "$BINDING_MAINTENANCE_OUTPUT" --apply \
  --approved-plan-sha256 "$BINDING_NATIVE_PLAN_SHA256"
```

Application rechecks the saved inputs, source, complete ConfigMap observation,
paused domain and state identity immediately before native Terraform apply. It
records `native-apply-pending` before launching that process. The saved binary's
SHA-256 must match the reviewed value; Terraform owns its ordinary backend lock
and state write. There is no direct ConfigMap patch, state rewrite, temporary
permission grant or force-unlock in this entrypoint.

After a timeout or lost response, preserve the directory and use `--observe` with
the same inputs. An attempted apply is never automatically repeated. Observation
records completion only when both the live ConfigMap and native Terraform state
contain the desired binding, with retained ownership and domain quiescence. If
they disagree, inspect the original Terraform process/provider children and its
native lock, then use the shared owner's normal recovery procedure. Do not reset
the receipt or use a second apply to guess whether the first completed.

`binding-and-native-state-observed` establishes the binding update only. Environment
variables in existing Gateway pods still require the normal shared-owner rollout
and current-process verification. Allow ordinary Gateway releases to continue;
do not restore an older image. Before enabling domain launch, verify the binding
in actual serving Gateway processes and qualify compatible Gateway/Harness and
scheduled-engine source through their maintained release paths.
