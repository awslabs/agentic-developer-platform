# Configuration ownership

L5 configuration, connection and execution permissions use the verified
workspace user's platform ID. A login provider's subject can differ from that
ID. The gateway resolves the authenticated human through the same current
membership authority as Product APIs before constructing an L5 backend.
Removed members remain denied even when their login token has not expired.

The request's authentication context is copied, preserving the original
verified subject for native UI federation. Registered service identities keep
their existing delegation path. This applies to every L5 service and Product.

Migration `170_l5_canonical_draft_owners` repairs legacy ownership only for
disabled, revision-one drafts with a unique same-workspace human identity match.
It excludes shadow identities, existing canonical user IDs, ambiguous matches,
and configurations with any binding, setup intent or run. Configuration IDs,
settings, revision hashes and idempotency request hashes remain unchanged;
the draft owner and matching receipt principal become the canonical user ID.

Existing execution history is never reassigned automatically. A legacy
configuration outside those constraints needs an explicit ownership recovery
review. The migration refuses downgrade because ownership and retained history
cannot safely be reversed.
