# GitHub setup identity and organization-control rollout

Applies to A10 (#5664): GitHub App install-start/install-callback and manifest
register-start/register-callback. This is source rollout guidance; it does not
record a live deployment, App permission change or completed verification.

## Setup state and user binding

The start routes resolve the signed Cognito subject to the canonical database
user. Installation state records that user and the selected workspace separately:
a workspace membership can legitimately refer to a User whose home tenant differs
from the selected workspace. An explicit workspace claim cannot fall back to a
user from another workspace when resolution fails.

Registration uses the canonical login user's current global platform-admin role.
A selected workspace's member or org-admin role cannot authorize registration of
the deployment's shared App. Native/bootstrap administrators can register without
a GitHub OAuth identity; registering an App and proving control of an installation
are separate authorized operations.

Both state namespaces now carry a versioned server-stored binding. **Pending
setup links issued by the previous code must be restarted after rollout.** They
expire after 15 minutes and lack the selected-workspace/registration-target
binding, so the callback cannot safely infer it. Return to Settings → Connections
and begin the relevant setup flow again. This does not rotate existing connections.

Callbacks intentionally authenticate the expiring, single-use capability returned
from authenticated issuance. They do not require a second browser JWT on GitHub's
redirect. Before mutation, they recheck the canonical subject/user binding,
workspace standing or global role, and expiry after provider calls. Registration
also checks the converted App's owner type and requested organization login.

## Installation account-control proof

The installation metadata must be fetched successfully from GitHub, identify the
requested installation, and provide a supported, unsuspended account with an
immutable numeric ID. Missing metadata or credentials cannot attach the
installation to a default tenant.

For a personal installation, GitHub's account type must be `User` and its ID must
match a proven GitHub identity for the initiating user **in the selected workspace**.
A Slack identity with the same text ID, a GitHub identity from another workspace,
a mutable username, or an unproven claim does not establish this control.

For an organization installation, an existing proven GitHub identity is sufficient
for the user-identity part. Accepted OAuth or administrator-approved immutable
identities do not require a fresh OAuth flow. The App installation token is used
to fetch the current login for that immutable user ID, followed by the current
organization membership. The response must have `state=active`, `role=admin`, and
the same immutable user and organization IDs. Installation visibility or ordinary
membership does not confer administrator control.

Existing account/installation ownership conflicts and tenant-standing failures
are checked before state consumption. Proof failures leave the setup state,
installation ownership, memberships, secrets and routing projections untouched.
Provider or permission failures are reported as unavailable verification and may
be retried while the state is still valid. A legitimate unproven identity needs a
supported proof-establishing enrollment; retries cannot manufacture that proof.

## Organization members permission

Organization membership-role verification requires the GitHub App's organization
**Members: Read-only** permission (`members: read`). Both setup sources request it:

- The UI manifest and expected-permission checker in
  `modules/gateway/src/admin/connections/service.py`.
- The CLI's displayed, requested and validated permission set in
  `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh`.

Existing registered Apps and existing installations do not gain this permission
from a source merge. During an authorized rollout:

1. Confirm the deployment's App and installation with the responsible operator.
2. Update that App's organization Members permission to Read-only through its
   normal configuration process. Preserve its other permissions and event choices.
3. Have the organization owner approve the installation's pending permission
   request. Changing the App's requested permission alone is insufficient.
4. Use a current installation token and a known proven organization administrator
   to verify the membership query, then complete a new setup flow.

The callback fails closed if the permission is absent, approval is pending, the
provider call fails, or GitHub cannot establish active administrator membership.
Do not relabel identity provenance, substitute ordinary membership, or bypass the
control check to complete an installation. If an environment cannot grant this
permission, organization setup through this App-token path remains unavailable;
this patch does not introduce a separate vault-credential or OAuth enrollment flow.

## Verification and limits

Validate same-ID and distinct Cognito-sub/database-ID accounts, native platform
registration, selected workspaces represented by local and foreign-home user
rows, personal ownership, and organization control with existing OAuth and
administrator-approved identities. Exercise denial for unproven/mismatched
identities, inactive/member-role or wrong-ID membership responses, revoked
workspace/global authority, expired state, and unavailable provider verification.
Check that denied calls leave state unused and no ownership/secret writes occur.

One-time state consumption is atomic per nonce. It does not make the subsequent
provider, database, Secrets Manager and projection operations one transaction, or
serialize different administrators' independently issued registration nonces.
Failures after an authorized mutation may require the existing reconciliation
procedure. Teardown and revocation implementation are reviewed separately; this
setup patch does not alter their source paths.
