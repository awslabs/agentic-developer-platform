# Story-bound repository migration and rollback

This procedure applies to gateway, scheduled tick and agent writers after the
repository-bound rollout in #8406. It is a **preflight**, not permission to deploy,
apply a migration or mutate live data. The assigned operator selects the target,
records the tested image digests privately and obtains the existing deployment
approval. Do not copy tenant IDs, URLs, account IDs, inventory or receipts into
this public document or a PR.

## Compatibility floor

The compatible-reader floor is `bound-repository-v1`: a **tested release image**
containing the bound gateway/tick/worker consumers and the authorization and
authoring-command changes from #8406. Verify the actual image digest and its
source/release manifest for every component before calling a candidate
`compatible`; an image tag, database revision or the diagnostic's caller-supplied
label alone is not proof. The old global-only gateway/tick binary is **below**
this floor even when the expanded columns exist. Never route new bound plans through
such a binary. Keep schema 131 and every subsequent additive field on rollback;
the 131 downgrade refuses even if there are no migration-decision rows.

## Before retiring the global input

The guarded full upgrade retains the legacy
`/adp/<environment>/gateway/orchestration-dispatch-repo` SecureString in SSM and
only removes its Terraform ownership. This happens in the early infrastructure
pass, while the old gateway can still be serving, and applies to both populated
and disabled values. Fresh installs create no global parameter. Retention does
not authorize repository attribution, clear pending effects or qualify a reader;
the checks below still govern binding migration. The new runtime does not use
the retained value as a routing fallback. Physical cleanup is deferred beyond
the release upgrade so neither old readers nor migration evidence lose it.

1. Record a private baseline of accepted plan bytes/hashes, version, decision
   IDs, node repository IDs/connections, claims/generations, pending dispatch
   envelopes and recovery receipts. Include outstanding SQL actions, tracker
   publication operations and protected DynamoDB dispatch/wave reservations.
   Limit access to the assigned operator; keep the original target and operation
   ID for each pending effect. An SQL inventory **does not** cover the protected
   dispatch store or other publication outboxes.
2. From a workspace with access to the selected gateway PostgreSQL database,
   run the installed gateway script with the tenant ID and a **new private output
   path**. Set `DATABASE_URL` through the approved private connection mechanism,
   not in command history or this document. Repeat per tenant; a `--flow-id`
   filter is for investigation, not a whole-installation clearance. Supply
   `--candidate-reader compatible` only after verifying all image digests; use
   `unknown` otherwise. The script reads under a read-only transaction and writes
   a mode-0600 snapshot without replacing an earlier one:

   ```bash
   python3 modules/gateway/scripts/legacy-binding-migration.py \
     --org-id '<tenant-id>' --output '<new-private-path>' \
   --candidate-reader unknown
   ```

   An `unknown` or `below-floor` candidate intentionally exits with status 2
   **after** writing the private snapshot; this is a writer refusal, not a lost
   inventory. Use a fresh path on every invocation.

3. Inspect the private output's `inventory` and `rollback` sections. Treat
   `legacy_binding_unresolved` as a hold, not a request to fill in the global
   value. Verify started provenance with the provider and existing approvals;
   use explicit version-checked amendments for untouched work. Missing Product
   attribution alone does not suspend an existing flow. Reconcile every listed
   `pending_action_ids` and `pending_publication_ids` against their recorded
   provider effects **before** retrying or retiring configuration. Separately reconcile outstanding other outbox,
   protected dispatch and wave effects with their original IDs. Re-run a new
   private inventory after reconciliation; keep both snapshots and receipts.
4. Roll out compatible readers and writers across gateway, tick and agents,
   verify bound multi-repository execution and recovery, then remove the retired
   global setting from fresh and preserved upgrade configuration. No old writer
   may overlap a new bound plan. The offline tests are not live qualification;
   #8408 owns the controlled deployment and acceptance.

## Upgrade failure or rollback

1. Stop new gateway and scheduled-tick admissions; quiesce the actual gateway,
   tick, agent and publication writers through the deployment operator's
   approved controls. Drain or account for in-flight work and verify the writer
   count is zero. Preserve queues, active assignments, claims, decisions,
   accepted plans, envelopes and all pending operations. Do **not** delete or
   reassign a pending effect to make a preflight pass.
2. Inspect migration state and the retained private inventories. If migration
   failed, inspect the committed Alembic revision and partial DDL before any
   retry. Do not run `alembic downgrade`, clear 131, restore an old global value
   as a routing fallback, or roll back to an unverified image.
3. Verify the candidate's gateway/tick/worker image digests against the
   `bound-repository-v1` floor. With `--candidate-reader below-floor` or
   `unknown`, the preflight retains its inventory but exits nonzero and records
   `writer_refusal: true`. **Keep all writers stopped**; restore a tested
   compatible image instead. Old binaries cannot learn this refusal from a
   database field, so stopping writers *before* the image change is mandatory.
4. For a verified compatible candidate, use the same preflight with
   `--candidate-reader compatible`. Preserve the expanded schema and deploy via
   the existing approved workflow; check its actual migration job. Resume only
   after confirming current provider ownership, assignments, claim generations,
   approvals and pending receipts still match the baseline. Retry outstanding
   operations with their original keys and verify no duplicate issue, PR or run.
   If identity, authorizations or pending effects cannot be reconciled, keep the
   affected work held and writers quiesced until the assigned operator resolves it.

The `rollback` section lists SQL action/publication IDs and unresolved node IDs, not a
complete installation-wide clearance or proof that an image is compatible. A
successful preflight cannot replace image verification, quiescence or the
operator's review of protected/publisher state.
