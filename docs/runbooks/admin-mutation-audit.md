# Admin mutation audit receipts

The gateway admits covered admin operations only after committing an `admin_operation_started` receipt to `security_audit_logs`. It commits one terminal receipt before returning success. A successful HTTP response includes `X-Admin-Operation-Id`; the same server-generated UUID is recorded as both operation ID and correlation ID. Caller correlation headers cannot replace it.

The mounted route manifest in `modules/gateway/tests/admin/admin_mutation_inventory.json` covers 79 operations: 56 write-method operations and two GitHub redirect callbacks using durable operation receipts; 19 persona/Bedrock operations retaining their existing writers; onboarding request/approve/deny are included in the durable operations; and two retired/read-only POST operations. The test mounts the actual production admin registration list and rejects manifest changes or removal of the durable route class. This is an admin-router inventory, not a claim about every gateway endpoint.

Normal operations use the authenticated token actor. Target tenant IDs are resolved from stored organization/user records or the service's resolved target. Tokenless GitHub callbacks begin with an unknown actor; their existing nonce authority checks supply the actor only after verification. Public no-nonce installs can remain unattributed to a human. Request bodies, nonce state, GitHub conversion codes, credentials, and exception messages are not copied into these receipts. Authentication failures before an authenticated operation begins remain owned by the authentication audit layer.

## Failure and reconciliation

An admission audit failure returns 503 before business effects. A terminal audit failure returns 503 with `admin_audit_reconciliation_required` and the operation ID. Already committed SQL/provider changes may have occurred. Do not automatically retry these operations: inspect authoritative state and the operation's receipts first.

Use the platform-admin-only `GET /admin/audit-events?unresolved_only=true` endpoint to retrieve admitted operations without an acknowledged success or safe refusal. Normal filters and pagination also apply. An operation with a partial/failure receipt remains in this list. There is deliberately no endpoint that silently marks an uncertain operation resolved.

Authorization/validation refusals before the service effect boundary record `denied`. Errors after reaching a potentially mutating service call record `reconciliation_required`, even if the returned HTTP status is 403/409/422. The boundary is deliberately conservative: a service may reject before changing anything. GitHub callbacks preserve their existing redirects and nonce rules; partial effects remain unresolved. Interrupted processes leave a durable pending intent. The receipts do not provide idempotency or an atomic transaction across PostgreSQL and external providers.

SQL pending in the request session is rolled back on handler failure. Successful handlers may already own a commit; the route finishes any remaining SQL and then persists the terminal receipt in a separate session. A missing terminal instrumentation call fails closed with 503 and leaves the intent visible. One idempotent tenant-link request still produces one terminal receipt per request, without duplicating the link.

## Verification scope

The durability tests mount real routes, use fresh SQL sessions after request teardown, and cover admission/terminal sink failure, rollback, permission refusal, provider partial failure, cross-org identity targets, callback success/replay/partial failure, idempotent linking, an existing persona writer, and removal of terminal instrumentation. Inventory mutations demonstrate that new unaudited endpoints and lost route wrappers fail the gate.

The callback authority and membership suites exercise the unchanged nonce/service security contracts. Provider/response unit tests with fake database objects explicitly stub only the receipt sink; they are not persistence evidence. The persistence suite uses SQLite; deployed PostgreSQL/IAM connectivity, live provider behavior, retention/export, and A11 onboarding acceptance require separate runtime evidence before closing the broader S13 story.

## Runtime acceptance checklist for the supervisor

- Deploy the reviewed gateway revision through its existing release process and record the image/revision and environment. Keep this separate from source merge evidence.
- In an authorized disposable tenant, exercise a SQL-only update, a provider-backed change, and a nonce callback. Record operation IDs and verify committed business state and exactly one terminal success from a separate PostgreSQL connection after request completion.
- Verify platform-admin retrieval and ordinary-user refusal; confirm actor and authoritative tenant match the controlled test subjects, including a platform admin acting across organizations.
- In a controlled fault environment, deny receipt insertion before an operation and confirm no business effect; interrupt/fail terminal persistence after an admitted operation and confirm 503/pending visibility without an automatic provider retry.
- Resolve or explicitly retain every pending fixture operation using inspected provider/SQL state. Confirm the intended audit retention and export access controls without exposing credential or callback material.
- Obtain the A11 owner's acceptance for the three onboarding handoff operations in the manifest. Re-run the production inventory gate after that merge and record any changed classification.
