# Access requests and applicable session revocation

Authentication and tenant membership are separate. `adp access status --tenant
TENANT_ID --json` reads server membership/request state without switching a
workspace or synchronizing memberships. `spend_eligibility: not_evaluated` is
intentional: a membership is not evidence of a usable budget or model route.

Request membership in an existing tenant:

```sh
adp access request --tenant TENANT_ID --reason 'Project access' --dry-run --json
adp access request --tenant TENANT_ID --reason 'Project access' --yes --json
```

Requests use the existing `/access/request` API and verified GitHub identity.
The same pending request is returned on retry; denial permits a new request.
This path never automatically grants a membership. A nonexistent tenant is
refused; creating a tenant remains the existing platform onboarding workflow.
The access command's `--tenant` identifies the requested target, including one
the caller cannot yet select. Other commands use global `adp --tenant ID ...`.

An administrator reviews exactly the target request:

```sh
adp admin access-request list --limit 50 --json
adp admin access-request show --request REQUEST_ID --json
adp admin access-request approve --request REQUEST_ID \
  --expected-revision REVIEW_REVISION --expected-role member \
  --expected-scope join_existing --operation-id UUID \
  --reason 'Reviewed project membership' --dry-run --json
```

Use `--yes` to submit those same reviewed values. `deny` accepts the same flags.
Pagination returns `next_cursor`; pass it as `--cursor` for the next bounded
page. The server owns role derivation and tenant authority. A stale request or
changed proposed grant returns conflict. Approved decisions persist a stable
operation receipt; an identical retry returns the recorded result. Reusing its
UUID for another payload is refused. If effects committed but the final receipt
was lost, the server requires reconciliation rather than executing again.
Existing UI decisions share the same database lock across service commits.

Review and revoke a disposable user's gateway-issued token family:

```sh
adp admin session revoke-user --user USER_ID --org TENANT_ID \
  --reason 'Disposable test complete' --dry-run --json
adp admin session revoke-user --user USER_ID --org TENANT_ID \
  --reason 'Disposable test complete' --expected-revision REVIEW_REVISION --yes --json
```

The existing token-revocation service marks the applicable gateway JWT records
revoked. Output includes revoked/remaining counts and the affected family.
Cognito login and refresh sessions are unchanged, established connections are
not forcibly closed, and hosted tasks are not stopped. A changed token-family
revision is refused. On lost delivery, inspect the same target with `--dry-run`
before deciding whether another operation is needed; never blindly resend.

Exit codes follow the shared contract: 1 usage, 2 authentication, 3 permission,
4 pending/conflict/uncertain delivery, 5 transport or invalid response. JSON uses
the shared envelope. A dry-run performs reads only; no confirmation grants
server authority.

Nightly E33 exercises installed CLI tenant access status and bounded admin
request review. Disposable approval/denial races, real membership/spend readback,
and actual client revocation timing remain live acceptance requirements. Source
implementation and offline tests do not close those requirements.
