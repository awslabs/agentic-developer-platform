# Native Cognito users: onboarding and recovery

Native ADP users do not need a GitHub identity. Their login must be linked to the
immutable Cognito `sub` in the deployment's configured pool. Organization/team
membership and roles remain ADP records; matching an email is never login proof.

The operations below require a platform administrator. Paths are gateway origin
paths. Prefix them with the default CloudFront `/api` base URL for public calls:

| Operation | Origin path prefix | Public path prefix |
| --- | --- | --- |
| Existing user/org creation | `/api/admin/identity/...` | `/api/api/admin/identity/...` |
| New provisioning retry and Cognito link | `/admin/identity/...` | `/api/admin/identity/...` |

The existing creation routes keep their client-compensated double prefix for
compatibility. New recovery routes follow the gateway convention: CloudFront
strips exactly one `/api` segment. Error response `retry_path` and `link_path`
values are origin paths; prepend the API base once.

## Create a new native user

`POST /api/admin/identity/organizations/{org_id}/users`

```json
{
  "email": "member@example.com",
  "name": "Member",
  "role": "member",
  "team_id": "team-in-this-org",
  "send_invite": true
}
```

A 201 response means Cognito returned an actual subject, ADP persisted the
subject and username, and the organization group sync succeeded. The response
includes `id`, `cognito_sub` and `cognito_username`. Ordinary members receive an
explicit ADP organization membership. The chosen team must belong to the org;
if omitted, the organization's default team is used.

With `send_invite: true`, Cognito sends its normal new-user invitation. With
`false`, invitation delivery is suppressed; the operator must arrange the
native Cognito password/login flow. The API does not create a permanent password.

## Retry a failed provisioning operation

A Cognito failure returns 502 with `error: "cognito_provisioning_failed"` and
`details.user_id`, `details.org_id`, `details.retry_path`, and `details.link_path`.
The ADP user row is retained so its ID, membership, and requested role remain
stable. Do not create a second user. Repeating the original POST returns 409
with the same recovery paths.

After resolving the AWS failure:

`POST /admin/identity/organizations/{org_id}/users/{user_id}/provision`

```json
{"send_invite": false}
```

Use `true` if the user still needs an initial invitation. A retry of an already
linked login verifies its stored subject and repeats the group sync; it never
resets a password or resends an invitation. If AWS created the login but its
response was lost before the subject could be saved, retry returns 409. Reconcile
that case with the explicit subject-link operation below. The same operation
repairs older unlinked ADP rows, including those created before #5010/#5011.

Org creation also requires its Cognito group to succeed. On a 502 group failure,
the org transaction is rolled back; retry the same org-create body after fixing
IAM. Group creation is idempotent.

## Link an operator-created Cognito user

Read the exact username and immutable `sub` from the **configured Cognito pool**
using an authorized AWS administration workflow. Then:

`PUT /admin/identity/organizations/{org_id}/users/{user_id}/cognito`

```json
{
  "username": "operator-created-native-login",
  "expected_sub": "dd545950-8d1a-421e-885d-b21312948c49"
}
```

ADP calls Cognito `AdminGetUser`, verifies the expected subject, rejects disabled
or missing users, and refuses to overwrite a different existing login owner.
Email can differ from the ADP display email. This operation does not reset a
password or copy client-supplied role/org claims into authority. An old row with
no membership receives only a member role; it does not inherit its display role.

To create the ADP row and link an existing Cognito login in one request, include
`cognito_identity` in the user-create body:

```json
{
  "email": "member@example.com",
  "role": "member",
  "cognito_identity": {
    "username": "operator-created-native-login",
    "expected_sub": "dd545950-8d1a-421e-885d-b21312948c49"
  }
}
```

This uses the existing login and sends no new invitation. If the same login is
already bound to an account in another ADP org, ADP records a verified workspace
link and preserves the unique canonical subject. Additional membership can also
be assigned through the normal platform roster placement flow. Fresh native
sign-in can then list/select these workspaces. Removing a secondary workspace
does not delete the shared login; deleting its canonical account is refused
while other organization memberships still rely on that login.

## Deployment and validation

Apply the gateway Terraform IAM change to grant only the required lifecycle
operations on the gateway's own user pool, then deploy the backend image using
the [canonical deployment guide](../adp-platform-deployment/deploy-with-agent.md).
No database migration is required. This change adds API recovery/link operations;
it does not add a dedicated browser recovery action.

Offline tests cover the database/API/workspace behavior and boto3 request/response
contracts. Live IAM, Cognito login, and the cross-account Bedrock scenario in
[#5008](https://github.com/aws-e/adp/issues/5008) still require validation after
deployment. Existing unlinked rows are not matched or repaired by email at login.
