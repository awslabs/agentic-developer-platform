# Governed account creation and bootstrap

This package implements #5531: create an AWS account through the admitted harness
operation, then establish the reviewed roles in the child account. It consumes the
Account Factory decision model and a trusted `DurableExecutor`; production service
composition and credential delivery belong to #5535.

`registration.create_from_durable_history` loads provider history through the live
`OperationExecutor.provider_calls` boundary before calling `create_account`; a caller
cannot supply history to this maintained composition. `create_account` requires a complete `ValidationAuthorization` matching the admitted
organization, workspace, management account and approved placement. The trusted
composer supplies `creation_hook` to the executor using its outcome vocabulary.
Credentials arrive through `CredentialSource`; this package constructs no SDK clients.

The executor commits intent before calling Organizations. The immutable operation
and creation generation determine the key, while the approved payload digest binds
the target. Changing the contact or OU cannot bypass an existing intent. A repeated
or concurrent dispatch is refused. A new generation is permitted only after a
persisted, explicitly retryable failure, within `MAX_CREATION_GENERATIONS`.

An `IN_PROGRESS` reply remains uncertain: its request ID is persisted while the
intent stays recoverable and budget stays retained. `reconcile_creation` reads that
stored ID and calls `DescribeCreateAccountStatus`, without another `CreateAccount`.
The trusted recovery composer must persist the observation through the harness
recovery contract. A lost response with no request ID remains unresolved and cannot
authorize an automatic retry.

`bootstrap_account` reads the child account before writing. Its reviewed plan binds
the account and operation, and requires the scoped role trust documents and permission
policy ARNs. Role creation and policy attachment use separate durable steps, so a
partial bootstrap can recover without creating the role again. Approved OU placement uses a separate durable `MoveAccount` step and an authoritative
parent read. Root and OU discovery handle nested units and pagination. Account closure
and deletion remain outside this package.

`registration.load_created_account` obtains the account ID from exactly one successful
creation generation of the current operation, checks every predecessor and payload
binding, verifies actual approved OU placement, and rechecks live authority after AWS
I/O. `render_created_account` passes that immutable record to Account Factory. The
renderer rejects free account-ID strings; the CLI cannot mint a registration record.

Bootstrap requires reviewed managed-policy documents as well as their ARNs. It reads
the default policy version before role attachment, creates missing policies through
separate durable steps, and refuses mismatches. The trusted service may supply explicit
`policy_update_arns` from the approved remediation plan to create a new default version;
it must never forward this field from untrusted request parameters. Bootstrap policy
permissions cannot rewrite their own privilege policy. Each role may attach only its
own reviewed policy, preventing cross-tier privilege assignment.

The runner sets all four account S3 public-access protections when an authoritative
read reports them missing. It verifies the selected `audit_trail_arn` directly through
CloudTrail, including multi-region/global coverage, integrity validation and logging
status. It does not create or alter an organization audit trail. Missing/denied reads
leave readiness unverified; caller-supplied `observed` baseline flags are refused.
Cloud adapters must translate only explicit not-found responses into `PolicyAbsent`;
a denied or unavailable read is never absence. A read-back after an interrupted policy
or S3 write settles the durable intent through the current leased executor before
reporting completion. Replays read established resources without repeating writes.

## Offline verification

From the repository root, in a Python 3.12 environment with the test dependencies:

```sh
python -m pip install pytest pytest-asyncio asyncpg pgserver pyyaml
(cd modules/domain-apps/superplane/infra/account-provisioning && \
  ACCOUNT_PROVISIONING_REQUIRE_POSTGRES=1 python -m pytest account_provisioning_tests/ -q)
(cd modules/domain-apps/superplane/infra/account-factory && \
  python -m pytest tests/ -q)
(cd modules/harness/jobs && \
  python -m pytest tests/ -q)
```

The tests use a unique `account_provisioning_tests` package so domain-wide collection
does not collide with Account Factory's `tests` package.

The PostgreSQL fixture creates a disposable local database; AWS clients are recording
doubles. The durability suite covers committed intent, concurrent dispatch, stale
leases, retry generations, and recovery using the persisted request ID. The shared
executor suite also checks that pending handles retain budget, preserve cancellation
cleanup, and cannot be written by an expired executor.

Before merge, run the applicable Domain, Harness and Gateway checks on the final
revision and obtain independent review. Offline tests and a merged code story do not
establish live account creation or installation acceptance. The Wave 6 evaluator
still needs the named organization/workspace and environment, scoped credential
references, approved placement/contact, reviewed bootstrap plan and policy artifacts,
and separate authorization for account creation or eventual closure. Record the
admitted operation, provider request/account IDs and observed bootstrap state as live
evidence; none is supplied by these synthetic tests.
