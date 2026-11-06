# Tenant-boundary rollout handoff — issue #7841

**Status:** pre-merge procedure, not an authorization or a deployment receipt.
The implementation PR is not proof that any image, migration, Cognito trigger or
client setting reached a target. Operations selects the environment and follows
the [canonical deployment guide](../../adp-platform-deployment/deploy-with-agent.md),
including its target-account confirmation and approval gate. Identity and
infrastructure owners authorize identity changes; the designated evaluator
alone records the two-tenant staging outcome ([S-12](validation.md#s-12--ac-08--manual-post-deployment-acceptance)).
No target identifiers, provider details, test accounts, logs or evidence belong
in this public file or the PR. Keep per-environment receipts in an
access-controlled operator store.

## Before any target change

1. Implementation owner and independent reviewer record final tested source
   and requirements revisions, component suite and migration/recovery results,
   CI/review disposition, and outstanding failures. Compare deployed artifact
   digests to that revision after delivery. A passing build is not acceptance.
2. Operations confirms the approved target, a supported staging lane and the
   delivery controls in [the ownership matrix](README.md#4-delivery-ownership-and-rollback).
   Main-branch Gateway and Door deployments may start automatically; Cognito
   infrastructure, Superplane API and migration apply require separate owner
   coordination. Do not merge expecting staging-first without confirming the
   workflow sequence.
3. Identity owners pause SSO mutations and privately inventory installed
   providers, exact ownership bindings, protected attribute writers, user
   membership and selected tenant, shared-client settings, approved HTTPS
   issuer origins, caller registrations and delegated operation bindings.
   Compare independent platform records with Cognito/provider state; provider
   display names, truncated IDs and IdP assertions do **not** prove ownership.
   Record unknown, shared or colliding ownership as unresolved. Do not guess a
   binding, delete/recreate a provider, or enable one with unknown owner.
4. In the private receipt, record for each intended change: environment and
   owner; before/after source, image, schema and trigger revisions; provider
   ownership evidence and collision check; client security settings and
   protected-attribute writer inventory; allowed issuer-origin decision;
   membership/caller/delegation reconciliation; planned change and dry-run
   result; apply approval and outcome; recovery checkpoint; S-12 evidence
   reference and cleanup disposition. Store secrets separately, never in the
   receipt. A blank or ambiguous field is a gate, not implied approval.

## Coordinated release and verification

- Preserve existing domain organization IDs and ADP tenant IDs; confirm
  translations and positive producer/executor controls before enforcing stricter
  Gateway reads. Roll out the verified caller path with its consumers rather
  than granting a shared service global tenant authority.
- Apply the additive Superplane ownership migration before an API that requires
  the new column. Keep old bindings and shared-client security settings intact.
  Reconcile legacy providers only on independent, exact-owner evidence while
  writes are paused, then retry idempotently on the *same* bound provider; an
  SDK failure or ambiguous candidate leaves that provider disabled/unbound.
  Resume mutations only after ownership and client settings are verified.
- Identity/infrastructure owners exclude tenant attributes from public IdP/client
  writes, grant the trigger read access to only its managed user pool, sanitize
  selected-tenant user records against current platform membership, then publish
  the fail-closed trigger. Check native recovery login, federated issue/refresh,
  workspace switch and missing membership before enabling federation; API
  admission must independently recheck current membership.
- Release Door ACL precheck and scoped catalogue lookup together; confirm
  allowed-empty repository diagnostics and foreign/unknown equivalence, plus
  refusal when ACL storage is unavailable. Operations monitors bounded denial
  reasons and healthy dependency probes without publishing private values.
  An unexpected denial increase or failed positive control stops the rollout.

Only after the owners confirm deployed digests, schema/trigger/client state and
healthy dependencies does the evaluator execute the complete [S-12 contract](validation.md#s-12--ac-08--manual-post-deployment-acceptance).
It requires two isolated run-owned tenants and repositories, real consumer
paths, paired own/foreign/absent checks, failure injection, private audit
correlation and native recovery. Poll readiness no more often than every 10
seconds, with a 15-minute deadline; elapsed time alone is not readiness.
The evaluator removes only run-owned fixtures through their supported owners,
verifies restoration and records any failed cleanup as incomplete acceptance.
Without a permitted target, IdP or private evidence, S-12 stays blocked.

## Stop and recover without restoring the vulnerability

On partial SDK failure, ownership ambiguity, failed migration, unhealthy
dependency or unexpected denials, halt dependent enablement and preserve the
private before-state receipt. Identity owners repair mappings and protected
writers while federation changes remain paused; rerun the exact-owner and
shared-client checks before retry. Operations rechecks image/trigger/schema
revisions and native recovery access before resuming. Do not turn off tenant
checks, allow unverified attributes, or re-enable an unbound legacy provider.

Keep additive schema columns through a secure application image rollback.
Only after exporting bindings privately and disabling all code that depends on
the column may the database owner consider a down migration. Never delete or
recreate installed providers as rollback. Roll back Gateway or Door only to a
previously verified **secure** artifact; if none exists, disable the affected
route or caller rather than reintroducing caller-selected tenancy or a private
repository existence oracle. Retain IdP write restrictions and the fail-closed
token projection even if federation is temporarily disabled. A failed recovery
or incomplete cleanup stays an open incident/acceptance item for its owner.
