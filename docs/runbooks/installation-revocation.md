# Installation revocation

Disconnect commits a durable local denial before attempting GitHub uninstall or
index cleanup. `installation_revocations` retains the owning tenant, recorded
installer IDs, original operation intent, provider confirmation, and pending
cleanup. Do not delete these records to clear an error: their absence would
remove both denial and retry authority.

The response distinguishes `local_revoked`, `provider_uninstall_requested`, and
`provider_revoked`. A provider timeout does not restore local access. Named
`residual` operations can be retried by the original installer or a platform
administrator in the owning tenant. The Connections page keeps pending
operations visible with a **Retry cleanup** button. A retry resumes the saved
intent; retrying a local detach never requests provider uninstall.

Operator detach is local-only. Removing a lone, unproven tenant assertion (or a
legacy metadata-only claim) also removes only that tenant's claim. It never
uninstalls at GitHub or publishes a global denial against another tenant's
installation. A global revocation record requires a sole canonical owner backed
by a server-written installation mapping.

The DDB `github_installation_revoked` marker is permanent and has no TTL.
Installation projection writes transactionally check that it is absent. Forward
and reverse cleanup use conditional deletes, so a replacement belonging to
another installation is preserved. Human identities, memberships and shared
credentials are independent and remain intact.

All webhook installation reads, auto-registration and reverse lookup/healing
also require a current canonical ownership answer with `revocation_checked=true`.
They deny on revoked, missing, mismatched, unreadable, or old-protocol answers.
This is deliberate: publishing the DDB marker can fail after SQL denial commits,
so an absent marker cannot authorize fallback during a gateway outage. It adds
an availability dependency on canonical installation resolution. The old
`RESOLVE_CANONICAL_VIA_GATEWAY` flag still controls the user-identity safety net;
it cannot disable installation revocation checks. Negative-cache expiration and
signed delayed lifecycle delivery cannot remove denial.

Deployment requires a fail-closed transition window: apply migration
`071_installation_revocation` and identity-index IAM permission
`dynamodb:ConditionCheckItem`, then deploy every webhook Lambda reader before
switching the gateway to the new teardown endpoints. New readers reject old
gateway responses lacking `revocation_checked`, so installation dispatch is
unavailable during that interval. Deploy the new gateway to restore dispatch,
then the frontend. Keep installation mutations quiesced until every reader and
gateway task uses the new version; an old reader cannot honor durable revocations.
Do not enable new teardown while any old reader remains. An alternative rollout
must quiesce installation traffic and mutations for the full mixed-version window.
This repository change does not perform deployment. Downgrade refuses to discard
active revocations; reconcile them explicitly before any rollback that removes
the table.

## Combined A10 rollout

When shipping revocation together with identity provenance enforcement, use a
full maintenance window. This sequence takes precedence over the standalone
writer/backfill/enforcement order in the
[identity provenance guide](identity-provenance-rollout.md):

1. Quiesce installation traffic and installation mutations for the entire window.
   Keep them stopped until every completion gate below passes.
2. Apply the release's joined migration head, including
   `071_installation_revocation` and `072_merge_identity_pricing`, and the
   identity-index `dynamodb:ConditionCheckItem` permission.
3. Deploy every enforcing webhook Lambda reader, then the new gateway, including
   all identity writers and canonical installation resolution. Old gateway
   responses are deliberately rejected during this interval.
4. Complete the provenance guide's exact-binding backfill and reconciliation
   gates for both required projection tables. Resolve incomplete results and
   pending older writes; a successful backfill alone is not an orphan or
   revocation audit.
5. Deploy the frontend and perform the approved checks for proven/unproven
   identity handling, current canonical installation resolution, local denial,
   and pending-cleanup retry behavior. Verify every reader and gateway task is
   on the reviewed release before resuming traffic or mutations.

Deploying readers before projection repair is safe only within this quiesced
window; it does not establish that legitimate user dispatch is ready. Resumption
requires both provenance reconciliation and the revocation protocol to be ready.
This sequence describes future authorized operations, not work performed by the
source change.

A genuinely new GitHub installation ID follows normal onboarding. Reusing a
locally detached ID requires the platform-admin attach endpoint's explicit
`restore_revoked=true`. It is allowed only for the same owner after local cleanup
has completed and no provider uninstall was requested. Ordinary callbacks and
auto-registration never clear denial. There is no automatic restoration UI.
