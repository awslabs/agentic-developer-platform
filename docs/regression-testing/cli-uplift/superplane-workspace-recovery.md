# Superplane workspace recovery prerequisite

`adp superplane workspace create --operation-id UUID` now accepts an externally
retained create identity. Preview reports the supplied UUID, malformed IDs fail
before transport, and confirmed creation passes the UUID to the existing
`replay_safe_create` path. Repeating the original arguments from a recreated
client sends the same operation ID and body. Existing local receipts still
refuse changed input or a different signed-in tenant/principal. Without the
flag, existing local receipt behavior is unchanged.

The evaluation helper `tests.e2e.cli_uplift.workspace_recovery` prepares and
retains an immutable basic workspace create intent through `Manifest` before
calling a transport callback. The intent includes the original gateway,
tenant/principal, ordinary session secret **name**, operation UUID, request
SHA-256 and exact CLI arguments. A missing or failed external manifest sink
prevents transport. Changed scope/payload is refused. The helper is a source
prerequisite and is not enabled as an E18 journey or cleanup deleter.

Offline regression covers acceptance with a lost response, loss of local CLI
receipts, reconstruction from external intent, the same operation/body on
replay, failed persistence, changed request/principal and read-only preview.
Those tests do not claim live provisioning or provider teardown.

## Current live hold

Current `POST /superplane/v1/workspaces` requires a reviewed plan revision and
starts governed provisioning. Workspace deletion likewise invokes provider
teardown. Neither is a metadata-only lifecycle. This change does not supply a
compute ceiling, an approval, provider cleanup proof or a complete planned
workspace CLI adapter. E18's remote and preflight recovery guards remain in
place. Do not use the basic create intent as authorization to provision.

Before enabling E18, finish the staged producer and scoped recovery deleters:
retain each approved operation before dispatch, persist each returned ID before
the next mutation, reauthenticate the original fixture after instance loss,
reconcile only the original operation, and verify provider/domain terminal
teardown separately from billing. An unknown result remains pending recovery;
name matching or a newly generated operation is not recovery.

E39 remains read-only until its owned metadata fixture and cleanup scenario are
wired and verified. Provider-connection registration now supports an explicit
operation UUID as its connection ID, so its existing scoped GET provides exact
recovery after a lost reply. Current authorization and original binding/reference
must still match on POST replay; disabled records are never reactivated. The
updated CLI requires the domain's explicit protocol feature before creation.
This closes the server-generated-ID lookup gap without adding compute admission.
It does not qualify actual vault/provider attestation, revoked credential cleanup
or any workload/billing lifecycle. A fabricated validation result must never be
used to turn pending registration into ready compute capacity.
