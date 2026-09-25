# Cluster authority scope contract (#6048)

Organization/workspace grants remain the maintained grant entities. An
`organization_grant_cluster_scopes` child grants explicit `cluster:use`,
`cluster:administer`, or `cluster:observe` on one cluster. None implies another.
The existing organization grant owns the opaque subject, human/service type,
and parent revocation. A parent with empty organization permissions may carry
cluster scopes; organization administration alone never grants cluster access.
Parent and child must both be live. Composite tenant foreign keys bind the
child to its parent grant and cluster. Existing `(org_id, principal)` uniqueness
is unchanged; no new identity directory or grant management API is introduced.

Migration 038 follows 037 and creates empty scopes. No grants are inferred from
organization/workspace administration, sharing flags, membership rows, installed
credentials, or observation secrets. Existing grant revocation revokes every
child scope. Re-enabling a parent also makes its unrevoked children live again;
permanent removal requires revoking the children. Scope generation is explicit
and must rotate when scope permissions are changed by a future supported writer.

Discovery and target selection require the strict API VerifiedCaller, exact
human/service type and subject, and the explicitly mapped selected ADP org.
The stored organization binding is checked on every call. Legacy callers and
unbound organizations fail closed. Discovery filters scopes before exposing any
cluster metadata or membership counts. Selection uses the same refusal for an
absent, foreign, ineligible, or unauthorized target. The current GET workspaces
route retains its organization-read prerequisite, plus cluster-use filtering;
this does not make a cluster-only scope sufficient for that route.

This verifies current local binding and grant state against verified claims; it
does not prove current ADP membership after token issuance. #6127 must supply
current ADP subject/type/selected-org resolution. Shared preview and runtime
remain disabled until that and the following interfaces are composed:

- Approval stores authenticated requester type and selected cluster identity,
  binds them to the approved operation, and rechecks requester cluster-use at
  decision, consumption, and membership reservation. Existing spend approval
  remains independently required. Legacy shared approvals missing evidence fail.
- Protected execution preflight verifies original operation authority and the
  executing service principal's own live cluster-use scope, with authenticated
  service/run delegation from ADP. It must not map an `agent` string or human
  requester's grant into service authority. `current_operation` propagates this
  check before effects and credential refresh, including revocation fencing.
- Shared observations reuse submitter authentication/signatures/sequence and
  receipts with explicit cluster-observe; membership or dedicated ownership
  cannot authorize fleet telemetry. Administration needs its own supported
  authority contract before exposing a grant/sharing mutation API.

A discovery result is neither approval nor durable effect authority. Scope
generation is not yet an approval projection. No worker, approval, observation,
or runtime permission is widened by this bounded storage/discovery change.
