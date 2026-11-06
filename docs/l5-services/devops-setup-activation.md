# DevOps setup consent and manual activation

A disabled DevOps draft can authorize creation of one native space or read-only
linking of one explicitly selected existing space. Setup consent does not authorize
investigation, Product content access, integrations, native UI access, or schedule
enablement. The default configuration remains disabled.

The Product owner uses the existing service APIs under
`/v1/products/{product_id}/projects/{project_id}/services/devops`:

1. Select DevOps in the Product Profile, verify the hosting connection, and select
   the current Product environment. Save an `investigation` configuration with
   `enabled: false` and native automation disabled. Choose `space_mode: create`,
   or `space_mode: adopt` with an exact `existing_space_id`. A separately configured
   `scoped_iam_federation` / `scoped_session` policy may remain saved; setup does
   not grant native UI access.
2. Read `GET /delegation-scope?for_control=true&control_action=devops_setup`.
   Review the exact owner, configuration revision/hash, connection version,
   account, region and expiry. Submit that digest to `POST /delegations` with
   `for_control: true`, `control_action: devops_setup`, the configuration ETag and
   an idempotency key.
3. Submit `POST /binding-operations` with the configuration revision and either
   `action: create`, or `action: link` with the exact `native_resource_ref` returned
   in the consent preview. Link consent binds that space ID, owner, Product/project,
   connection revision, account/Region and configuration hash. Keep the original
   operation and key; another create/link key cannot replace them.
4. After setup succeeds, conditionally save the configuration with `enabled:
   true` and one manual investigation trigger selecting that binding, the current
   environment and a nonempty authorized resource selection. The existing
   investigation input contract remains unchanged.
5. Revoke the setup grant. Review and issue ordinary execution consent for the
   new configuration revision and exact binding before submitting a run.

Creation consent contains only `binding:create` and `credential:session`.
Link consent contains only `binding:link` and `credential:session`. Every
setup admission and broker request checks its purpose, current owner membership,
configuration, provider connection, durable intent, lease and fence. The native
creation policy permits fingerprint-tagged space creation, bounded reads, and
disabling observed goals on that created space. The linking policy permits only
`GetAgentSpace` and `ListGoals` on the exact selected ARN. It cannot create spaces,
change goals, submit tasks, manage integrations or IAM, open native UI sessions,
or delete resources.

Activation requires the original successful setup operation: a matching creation
receipt for an ADP-created space, or the exact successful link for a customer space.
The durable adoption record remains `customer_linked`, without a fabricated
creation receipt or cleanup rights. Later checks and configuration revisions
refresh its observations while retaining the original link identity.
Activation also needs a native goal readback from the last 15 minutes confirming that
every observed goal is disabled. Missing, empty, stale or mismatched goal evidence
fails closed. It also requires a current selected environment, the exact verified
hosting connection, a finite USD planning estimate,
one provider attempt, concurrency one, a deadline from 60 to 3,600 seconds and
1–60 polls. Saving the configuration makes no native call and queues no run.
Manual investigations require no budget setup or budget reservation. Run admission
still checks current execution consent. The cost estimate is not a provider billing cap.

The initial activation transition does not enable integrations, incident
automation, prevention, release gates or native browser access. Existing separate
SSO settings must remain unchanged. An enabled goal on an adopted space keeps the
binding blocked; linking never pauses it. After a separately authorized owner
change disables the goal, a fresh resource-verification check can make the same
binding ready. Unstarted expired link consent can use the same explicit authority
continuation as creation; started work cannot be restarted through that path. Configuration
readiness is not whole-service qualification. Product AWS discovery currently
exposes verified environment selections; an environment reference is not evidence
of an exact deployed workload. Real workload acceptance additionally needs a
supported resource-registration/readback path and its retained native evidence.

`setup_already_retained` identifies the existing operation that must be reused.
`setup_provenance_unavailable` requires a successful retained setup receipt.
`native_goal_readback_stale` requires a fresh native observation; changing a
configuration flag or supplying client-authored evidence cannot satisfy it.
Consumers remain disabled until their separate deployment and acceptance gates
are completed.

Setup persistence retains the intent before its operation and work rows, all in
one transaction. A child constraint failure rolls back the entire request,
including a control operation's binding revision/generation change. A true
duplicate-key race returns `idempotency_conflict`; other database integrity
failures return `setup_persistence_failed` and a safe diagnostic event for
platform support. Keep the original request key, repair the storage/runtime
problem, and retry that request. Changing the key does not repair a persistence
failure and can lose the connection to an already retained operation.
