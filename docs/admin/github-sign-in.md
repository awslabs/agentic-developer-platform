# GitHub Sign-In — Admin Guide

This guide covers how to configure, enable, and manage GitHub-based sign-in for an ADP deployment. The target audience is platform operators, not end users.

## Overview

ADP supports GitHub as a federated identity provider alongside the default email/password authentication. When enabled, users see a "Sign in with GitHub" button on the login page. Under the hood, this uses Cognito's external identity provider integration with a GitHub OAuth App.

**How it works:**

```
User clicks "Sign in with GitHub"
        │
        ▼
Cognito Hosted UI ──► GitHub OAuth authorize
        │                       │
        │              User approves access
        │                       │
        ▼                       ▼
Cognito receives GitHub token
        │
        ▼
Pre-Sign-Up Lambda (allowlist check)
        │
        ▼
Pre-Token-Generation Lambda (inject custom claims)
        │
        ▼
User receives Cognito tokens (same as email/password flow)
```

## Prerequisites

1. **A deployed ADP gateway** — Cognito User Pool, frontend, and backend must be running.
2. **GitHub organization** — Required if using `org` allowlist mode.
3. **AWS CLI access** — You need permissions to modify Cognito, Secrets Manager, and Lambda.
4. **Terraform** — The identity provider is managed via Terraform variables.

## Step 1: Register a GitHub OAuth App

1. Go to **GitHub** > **Settings** > **Developer settings** > **OAuth Apps** > **New OAuth App**
   - If scoping to an org: go to the org's **Settings** > **Developer settings** > **OAuth Apps**

2. Fill in the registration form:

   | Field | Value |
   |-------|-------|
   | Application name | `ADP Gateway (<environment>)` |
   | Homepage URL | `https://<your-cloudfront-domain>` |
   | Authorization callback URL | `https://<cognito-domain>.auth.<region>.amazoncognito.com/oauth2/idpresponse` |

   The callback URL must point to Cognito's IdP response endpoint, **not** your application's callback URL.

3. After creation, note the **Client ID**.

4. Click **Generate a new client secret** and copy it immediately (it won't be shown again).

> **Finding your Cognito domain:** Run:
> ```bash
> aws cognito-idp describe-user-pool \
>   --user-pool-id <POOL_ID> \
>   --query 'UserPool.Domain' --output text
> ```
> The full callback URL is: `https://<domain>.auth.<region>.amazoncognito.com/oauth2/idpresponse`

## Step 2: Store Credentials in Secrets Manager

Store the GitHub OAuth App credentials so Terraform and the platform can reference them:

```bash
aws secretsmanager create-secret \
  --name "adp/<environment>/cognito/github-oauth" \
  --description "GitHub OAuth App credentials for Cognito IdP" \
  --secret-string '{
    "client_id": "<GITHUB_CLIENT_ID>",
    "client_secret": "<GITHUB_CLIENT_SECRET>"
  }' \
  --region <region>
```

## Step 3: Configure Terraform Variables

Add the following variables to your environment's gateway tfvars file (e.g., `environments/dev/modules/gateway.tfvars`):

```hcl
# GitHub Sign-In Configuration
github_oauth_enabled    = true
github_oauth_secret_arn = "arn:aws:secretsmanager:<region>:<account-id>:secret:adp/<env>/cognito/github-oauth-<suffix>"

# Allowlist mode: "org", "explicit", or "open"
github_auth_allowlist_mode = "org"

# For "org" mode: users must be members of this GitHub organization
github_auth_allowed_org = "your-github-org"

# For "explicit" mode: list of allowed GitHub usernames
github_auth_allowed_users = ["user1", "user2", "user3"]
```

Then apply:

```bash
cd modules/gateway/infra
terraform plan -var-file=../../../environments/dev/modules/gateway.tfvars
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars
```

This creates:
- A `aws_cognito_identity_provider` resource for GitHub
- Updates the User Pool Client's `supported_identity_providers` to include GitHub
- Deploys/updates the Pre-Sign-Up Lambda with allowlist logic
- Updates callback URLs to include the Cognito hosted UI IdP response endpoint

## Step 4: Update Frontend Callback URLs

Ensure your Cognito User Pool Client's callback URLs include both:
- `https://<cloudfront-domain>/auth/callback` (your app's OAuth callback)
- `http://localhost:5173/auth/callback` (local development, if needed)

These are managed via the `callback_urls` Terraform variable:

```hcl
callback_urls = [
  "https://<cloudfront-domain>/auth/callback",
  "http://localhost:5173/auth/callback"
]
```

## Allowlist Management

The Pre-Sign-Up Lambda controls who can sign in via GitHub. Three modes are available:

### Mode: `org` (Recommended for teams)

Only members of a specific GitHub organization can sign in.

```hcl
github_auth_allowlist_mode = "org"
github_auth_allowed_org    = "your-github-org"
```

The Lambda calls the GitHub API to verify org membership using the user's access token during sign-up.

**To change the org:** Update the Terraform variable and apply. Existing users are not affected; the check only runs on first sign-up.

### Mode: `explicit` (Strict control)

Only GitHub users whose username appears in a predefined list can sign in.

```hcl
github_auth_allowlist_mode  = "explicit"
github_auth_allowed_users   = ["alice", "bob", "charlie"]
```

**To add a user:** Append their GitHub username to the list and run `terraform apply`.

**To remove a user:** Remove their username from the list and apply. This blocks future sign-ups but does not revoke existing sessions. To fully revoke:

```bash
# Find the user in Cognito
aws cognito-idp list-users \
  --user-pool-id <POOL_ID> \
  --filter "username = \"GitHub_<github-user-id>\""

# Disable or delete
aws cognito-idp admin-disable-user \
  --user-pool-id <POOL_ID> \
  --username "GitHub_<github-user-id>"
```

### Mode: `open` (No restrictions)

Any GitHub user can sign in. Use only for internal/demo deployments.

```hcl
github_auth_allowlist_mode = "open"
```

### Switching Between Modes

Change the `github_auth_allowlist_mode` variable and apply. The Lambda reads the mode from its environment variables, so changes take effect immediately after deployment (no user pool recreation needed).

## Troubleshooting

### Callback URL mismatch

**Symptom:** `redirect_mismatch` error after GitHub authorization.

**Cause:** The callback URL registered in the GitHub OAuth App doesn't match the Cognito IdP response URL.

**Fix:** Verify the OAuth App's "Authorization callback URL" is exactly:
```
https://<cognito-domain>.auth.<region>.amazoncognito.com/oauth2/idpresponse
```

Find your Cognito domain:
```bash
aws cognito-idp describe-user-pool \
  --user-pool-id <POOL_ID> \
  --query 'UserPool.Domain' --output text
```

### Org membership denied

**Symptom:** User sees "PreSignUp failed with error: User is not a member of the allowed organization."

**Cause:** The user is not a member of the configured GitHub org, or their membership is private and the OAuth App lacks permission to see it.

**Fix:**
1. Confirm the user is a member: `gh api /orgs/<org>/members/<username>`
2. If membership is private, the user must explicitly grant the OAuth App access to their org membership. They can do this at `https://github.com/settings/connections/applications/<client-id>`.
3. Alternatively, make the membership public in the org's People page.

### Token exchange failures

**Symptom:** `invalid_grant` or `invalid_client` errors in Cognito logs.

**Cause:** Client secret mismatch, expired secret, or the OAuth App was regenerated.

**Fix:**
1. Verify the secret in Secrets Manager matches the GitHub OAuth App:
   ```bash
   aws secretsmanager get-secret-value \
     --secret-id "adp/<env>/cognito/github-oauth" \
     --query 'SecretString' --output text | jq .client_id
   ```
2. If the secret was rotated on GitHub, update Secrets Manager and run `terraform apply` to propagate.

### User created without custom attributes

**Symptom:** GitHub user signs in successfully but has no `org_id`, `team_id`, or `role` in their token.

**Cause:** External identity provider users are created in Cognito without custom attributes. They must be assigned to an org by an admin.

**Fix:** An admin must assign the user to an organization:
```bash
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <POOL_ID> \
  --username "GitHub_<github-user-id>" \
  --user-attributes Name="custom:org_id",Value="<org-id>" \
                    Name="custom:team_id",Value="<team-id>" \
                    Name="custom:role",Value="user"
```

Or use the admin API: `POST /admin/users/<user-id>/assign-org`.

### Pre-Sign-Up Lambda errors

**Symptom:** Sign-in fails with a generic "PreSignUp failed" message.

**Debug:**
```bash
# Check Lambda logs
aws logs tail /aws/lambda/<name-prefix>-pre-sign-up --since 5m --follow

# Check Lambda configuration
aws lambda get-function-configuration \
  --function-name <name-prefix>-pre-sign-up \
  --query 'Environment.Variables'
```

Common causes:
- `ALLOWLIST_MODE` env var not set (defaults to deny-all)
- `ALLOWED_ORG` set but GitHub API rate-limited (Lambda needs a GitHub token for org checks)
- Lambda timeout too short for GitHub API calls (recommend 10s)

## Security Considerations

### Token lifetime

GitHub sign-in users receive the same Cognito tokens as email/password users:
- **Access token:** 60 minutes (configurable via `access_token_validity`)
- **ID token:** 60 minutes (configurable via `id_token_validity`)
- **Refresh token:** 30 days (configurable via `refresh_token_validity`)

The GitHub OAuth token is used only during the initial sign-up/sign-in flow by Cognito. It is not stored or used by the application after authentication completes.

### Session management

- Sessions are managed entirely by Cognito, not by GitHub.
- Revoking the GitHub OAuth App connection does **not** invalidate active Cognito sessions.
- To force logout a user, use:
  ```bash
  aws cognito-idp admin-user-global-sign-out \
    --user-pool-id <POOL_ID> \
    --username "GitHub_<github-user-id>"
  ```

### Revoking access

To completely revoke a GitHub user's access:

1. **Disable the user in Cognito** (prevents new token issuance):
   ```bash
   aws cognito-idp admin-disable-user \
     --user-pool-id <POOL_ID> \
     --username "GitHub_<github-user-id>"
   ```

2. **Force sign-out** (invalidates existing tokens):
   ```bash
   aws cognito-idp admin-user-global-sign-out \
     --user-pool-id <POOL_ID> \
     --username "GitHub_<github-user-id>"
   ```

3. **Remove from allowlist** (if using `explicit` mode): Remove the username from `github_auth_allowed_users` and apply Terraform.

4. **Delete the user** (permanent, removes all data):
   ```bash
   aws cognito-idp admin-delete-user \
     --user-pool-id <POOL_ID> \
     --username "GitHub_<github-user-id>"
   ```

### Attribute mapping

When a user signs in via GitHub, Cognito maps these attributes:

| GitHub attribute | Cognito attribute | Notes |
|-----------------|-------------------|-------|
| `login` | `preferred_username` | GitHub username |
| `email` | `email` | Primary verified email |
| `name` | `name` | Display name (may be empty) |
| `id` | `sub` (external) | GitHub user ID (numeric) |

The Cognito username for GitHub users follows the pattern: `GitHub_<github-numeric-id>`.

### MFA considerations

- GitHub sign-in bypasses Cognito's MFA enforcement (MFA is handled by GitHub itself).
- If your org requires MFA, enforce it at the GitHub org level (Settings > Authentication security > Require two-factor authentication).
- This is acceptable because the trust boundary is "GitHub has authenticated this user" — if GitHub's MFA is enabled, the user has already completed a second factor.

### Least privilege for the OAuth App

The GitHub OAuth App should request minimal scopes:
- `read:user` — Read user profile (username, email)
- `read:org` — Required only if using `org` allowlist mode (to verify membership)

Do **not** grant `repo`, `write:org`, or any write scopes. The OAuth App is used solely for identity verification.

## Disabling GitHub Sign-In

To disable GitHub sign-in without removing the configuration:

```hcl
github_oauth_enabled = false
```

Run `terraform apply`. This removes the identity provider from Cognito but preserves the Secrets Manager entry and existing user accounts. Users who previously signed in via GitHub can no longer use that method but their Cognito accounts remain (they would need a password reset to use email/password).

To fully remove:
1. Set `github_oauth_enabled = false` and apply
2. Delete the Secrets Manager secret: `aws secretsmanager delete-secret --secret-id "adp/<env>/cognito/github-oauth" --force-delete-without-recovery`
3. Optionally delete GitHub-linked users from Cognito

## Reference

- [AWS Cognito: Adding social identity providers](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-social-idp.html)
- [GitHub OAuth Apps documentation](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)
- [Cognito Pre-Sign-Up Lambda trigger](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-sign-up.html)
- Gateway auth implementation: `modules/gateway/src/auth/`
- Cognito Terraform module: `modules/gateway/infra/modules/cognito/`
- Frontend auth hook: `modules/gateway/frontend/src/contexts/AuthContext.tsx`
