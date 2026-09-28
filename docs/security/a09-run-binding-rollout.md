# A09 authenticated run binding

Credential brokers require verified IAM transport plus the canonical run and
workload credentials, current execution authorization and exact origin binding.
Request invocation, user, installation and repository fields are assertions,
never selectors that establish ownership. Former shadow flags cannot bypass
these checks. Shared review report credentials retain their separate broker
contract. Provenance additionally binds actor, tenant, correlation, human-rooted
state, root, parent and trigger to the authenticated execution's origin event.

Ingestion dispatch requires signing material and an unreserved registered asset.
It commits the callback attempt UUID and token digest before SQS publication.
Callbacks require the signed asset, explicit tenant (including shared NULL),
current attempt, token digest and a nonterminal status. Terminal completion or
failure cannot be replayed; an authorized reindex invalidates the old attempt.
An SQS error is an uncertain outcome: the attempt remains reserved and dispatch
will not replay it. An explicit reindex creates a new attempt. If the first send
actually delivered, its callback can complete the reserved attempt until then.

## Deployment prerequisites

1. Apply agent-context migration `013_ingestion_callback_attempt` before gateway
   code uses its columns. The migration does not authorize historical work.
2. Configure `AGENT_RUN_CREDENTIAL_KEY` on the gateway. Without it dispatch
   refuses to enqueue unsigned work. Preserve the key needed by in-flight signed
   attempts until those attempts finish or are explicitly invalidated.
3. Deploy ingestion producers that forward `callback_grant` unchanged from the
   queue through every callback. Unsigned and `adpk1` historical messages do not
   acquire authority automatically; drain them before activation or reindex via
   the authenticated asset route after the rollout.
4. Migrate every broker and provenance producer to canonical run/workload
   credentials over verified IAM transport. Existing Lambda or legacy worker
   provenance writers that carry only a shared secret are refused. Ensure origin
   rows contain the exact actor and attribution used by each producer; missing
   facts cannot be replaced by caller claims or membership-policy defaults.
5. Validate private and explicit shared ingestion, a legitimate credential
   operation, and truthful human/service provenance in the rollout environment.
   Service provenance must reference its server-recorded service root, which the
   existing schema requires to be a users-row FK despite its historical name.

This PR supplies code and local synthetic/PostgreSQL verification. It does not
assert live gateway activation, producer migration or production acceptance.
