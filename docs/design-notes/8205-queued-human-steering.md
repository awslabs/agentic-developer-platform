# Human steering across delayed model boundaries

Refs #8205.

A reviewer may wait for CI for longer than the control forwarding proof's
30-second lifetime. The human login can still be valid when the reviewer next
accepts input. Reusing the expired proof previously rejected that instruction.

Human `steer` now has two separate decisions:

1. The authenticated human route verifies current ownership and membership. It
   creates an immutable queued-command grant in the existing protected authority
   table and signs its opaque reference into the normal 30-second admission
   envelope. The worker must verify that envelope while it is current. Neither
   the envelope lifetime nor verification rules change.
2. At the model input boundary, the worker sends the grant reference and exact
   original command bytes to the existing authenticated revalidation route. It
   does not send the admission envelope. The gateway rechecks the original
   canonical actor, tenant, target generation, attempt, target grant ID and
   revocation epoch, current membership, and original session deadline. Legacy
   sessions also recheck the original `tokens` row for revocation and expiry.
   The gateway atomically consumes the grant and returns a decision usable only
   within the existing one-second round trip and handoff limit. More than one
   second must remain in the original human session after the consume.

The grant contains hashes and identifiers, with no bearer/JWT, signing key, or
instruction text. A later login cannot replace or extend the original grant.
The command marker has no TTL: deleting it could allow the same command ID to
be recreated with renewed authority. Any future retention procedure must retain
the refusal marker until the target execution can no longer accept that
generation. Existing grant and membership revocation remain authoritative.

Cognito's current locally validated JWT path does not verify provider global
sign-out. This change preserves its existing membership and target-grant
revocation checks and original JWT expiry; it does not claim new Cognito
session-revocation support.

The consume and original worker journal permit at most one handoff. A duplicate
or concurrent delivery request cannot consume again. A definitive refusal is
`rejected`; an ambiguous response, timeout, malformed decision, or boundary
closing after consumption is terminal `unknown`, with no handoff or automatic
retry. Delivery acknowledgement still means physical SDK handoff, not model
comprehension. If an allowed response is lost after the consume, the instruction
can remain undelivered; safety takes precedence over silently replaying it.

The public CLI command and status schemas remain unchanged. Delegated controls
and human pause/resume/abort keep their existing proof path. A new worker accepts
old-format envelopes. An old worker ignores the new reference and retains the
old 30-second delayed-delivery behavior. Its still-current new-format envelope
converges on the same one-use consume, so compatibility cannot leave a second
delivery route available. Live delayed-steer acceptance therefore
requires compatible gateway and worker code. No previously rejected command is
revived, and this source change does not mutate running workers or submit a new
control command. Old workers also retain their existing boundary-loss and
transport-failure diagnostics; the new `unknown` classifications require the
updated worker.

Tests cover a simulated fourteen-minute wait through the gateway route, expired
admission proofs, original session expiry including the handoff edge, membership
and target-grant revocation, changed grant epoch, legacy token revocation through
real authentication, stale generation, foreign principal/worker, changed command
bytes, concurrent consumption, duplicate/lost replies, worker boundary loss,
and the unchanged proof contract.
