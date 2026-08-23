# GitHub Auth Broker — Allowlist Remediation

**Subsystem:** Gateway / GitHub auth broker (Cognito)
**Issue:** #3986 (Group B of sub-EPIC #3984)

## Why this runbook exists

Before #3986 the broker's `ALLOWLIST_MODE` defaulted to `open`, and
`allowlist.check_org_membership` returned `True` when no orgs were configured.
On the shipped defaults **any GitHub user who reached `/auth/github/start` got a
fully provisioned Cognito user and a valid session** — the broker calls
`admin_create_user`, which does not fire `PreSignUp_ExternalProvider`, so the
pre-signup org gate never applied.

#3986 makes the gate fail closed. That stops **new** unauthorized accounts but
does **not** retroactively remove accounts provisioned during the open window.
Those accounts are fully provisioned (`admin_create_user` +
`MessageAction=SUPPRESS` + a permanent password), so they must be audited and
disabled explicitly.

Fail-closed also blocks re-authentication for those users — each broker login
rotates the Cognito password (`cognito_provisioner.py`) — but the account, its
attributes, and any derived Postgres rows survive until you act.

## Emergency recovery: everyone is locked out right now

**Symptom:** every GitHub sign-in (including yours) is denied after a broker
deploy. `/api/health` is healthy and the gateway pods are Running, so this is
**not** a CloudFront `/api` outage. Broker CloudWatch logs show, for each login
attempt:

```
[ERROR] ALLOWLIST_MODE=open without ALLOW_OPEN_SIGNUP=true is a misconfiguration; denying sign-in
```

**Cause — the code-before-config lockout window.** The broker's Lambda **code**
publishes on merge (see the deploy sequence below, step 1), but the
`ALLOW_OPEN_SIGNUP` environment variable only lands when `gateway-infra-apply.yml`
runs (step 2). If your environment was on `ALLOWLIST_MODE=open` and the #3986 code
reaches it before the config apply, the new code sees `open` **without** the
acknowledgement flag and fails closed — denying everyone until the apply catches
up. Any environment still on `mode = open` is armed to hit this on its next broker
republish.

**Immediate fix (restores login in ~30s):** add the flag to the live function so
the running code stops denying. This is the exact value Terraform will set on the
next apply, so it is not a divergent hack — but it *is* untracked drift until the
apply runs, so do step 2 of the deploy sequence promptly afterward.

```bash
ENVIRONMENT=dev   # your environment
aws lambda get-function-configuration \
  --function-name "bedrockgw-${ENVIRONMENT}-github-auth-broker" \
  --query 'Environment.Variables' > /tmp/broker-env.json
# add "ALLOW_OPEN_SIGNUP": "true" to the map in /tmp/broker-env.json, then:
aws lambda update-function-configuration \
  --function-name "bedrockgw-${ENVIRONMENT}-github-auth-broker" \
  --environment "Variables=$(jq -c . /tmp/broker-env.json)"
```

Then confirm the flag is live and retry your login:

```bash
aws lambda get-function-configuration \
  --function-name "bedrockgw-${ENVIRONMENT}-github-auth-broker" \
  --query 'Environment.Variables.ALLOW_OPEN_SIGNUP'   # expect "true"
```

> **Preferred posture, not just recovery.** `open` disables allowlist enforcement
> entirely. Unless open signup is genuinely intended for this environment, the
> durable fix is to move it to `mode = org` (or `explicit`) via the deploy
> sequence below — not to leave `ALLOW_OPEN_SIGNUP=true` in place. The hand-patch
> above buys time; it is not the end state.

## Deploy sequence (do this first, in order)

The broker's Lambda **code** and its Terraform **configuration** ship through two
independent paths. Both are required; the code path fires on its own.

> **Order matters — do not merge and walk away.** Step 1 (code) fires
> automatically on merge; step 2 (config) is manual. On an environment currently
> set to `ALLOWLIST_MODE=open`, the window between them is a **total login
> outage** (see *Emergency recovery* above). Run step 2 immediately after the
> merge, or pre-set `github_auth_allow_open_signup = true` in that environment's
> tfvars **before** the code reaches it.

1. **Merge the PR.** `.github/workflows/github-auth-broker-deploy.yml` fires
   automatically on any push touching
   `modules/gateway/lambda/github-auth-broker/**` and updates the live
   `bedrockgw-<env>-github-auth-broker` function code. No action needed.
   Manual fallback if it does not run: `modules/gateway/scripts/deploy-broker.sh`.

2. **Run `gateway-infra-apply.yml` manually.** This applies the new Terraform var
   defaults and the `environments/dev/modules/gateway.tfvars` values.
   `ALLOWLIST_MODE` is **not** in the broker's `lifecycle.ignore_changes`
   (unlike `filename`, `source_code_hash`, `GITHUB_CLIENT_ID`, `CALLBACK_URL`),
   so the flip does take effect on apply.

   ```bash
   gh workflow run gateway-infra-apply.yml
   ```

3. **Confirm the live configuration:**

   ```bash
   ENVIRONMENT=dev
   aws lambda get-function-configuration \
     --function-name "bedrockgw-${ENVIRONMENT}-github-auth-broker" \
     --query 'Environment.Variables.{mode:ALLOWLIST_MODE,orgs:ALLOWED_ORGS,open:ALLOW_OPEN_SIGNUP,token:GITHUB_TOKEN_SECRET_ARN}'
   ```

   Expect `mode=org`, a non-empty `orgs`, and `open` absent or `false`. If
   `mode=open` and `open=true`, allowlist enforcement is **disabled** — that is
   the deliberate escape hatch and should not be set in a shared environment.

> **Operational warning.** Any deployment that relied on the old open default
> must now set `github_auth_allowlist_mode` and `github_auth_allowed_orgs`
> explicitly. Terraform validation rejects `mode = "org"` with an empty org list
> and rejects `mode = "open"` unless `github_auth_allow_open_signup = true`, so a
> misconfiguration fails at plan time rather than silently at login.

## Audit existing broker-provisioned users

Broker-provisioned users are named `GitHub_<numeric-id>` in code, but **Cognito
stores the username lowercased** — the real values are `github_<numeric-id>`.
Match case-insensitively or you will find nothing.

The GitHub login is kept in the `custom:github_username` attribute, which is what
you cross-check against the allowlist.

1. **List every broker-provisioned user with its login and enabled state:**

   ```bash
   POOL_ID=$(aws ssm get-parameter --name /adp/dev/gateway/cognito-user-pool-id \
     --query Parameter.Value --output text)

   aws cognito-idp list-users --user-pool-id "$POOL_ID" \
     --query 'Users[?starts_with(Username, `github_`)].[Username,Enabled,Attributes[?Name==`custom:github_username`].Value | [0]]' \
     --output text
   ```

   Paginate with `--limit 60` plus `--pagination-token` on large pools.

2. **Determine which logins are actually allowed.** For each login from step 1,
   check membership in every org in `ALLOWED_ORGS` using a token with `read:org`:

   ```bash
   ORG=aws-e
   LOGIN=<github-login>
   gh api "orgs/${ORG}/members/${LOGIN}" --silent && echo "MEMBER" || echo "NOT A MEMBER"
   ```

   `gh api` returns 204 for a member and 404 for a non-member. A 302 or 403 means
   your token lacks `read:org` or the org has not approved it — that is
   "cannot verify", **not** "not a member". Fix the token before disabling anyone.

3. **Disable each confirmed non-member.** Disable rather than delete on the first
   pass so the action is reversible and the audit trail survives:

   ```bash
   aws cognito-idp admin-disable-user --user-pool-id "$POOL_ID" --username "github_<id>"
   ```

   To reverse: `aws cognito-idp admin-enable-user --user-pool-id "$POOL_ID" --username "github_<id>"`.

4. **Optionally delete** once you are satisfied the disable list is correct:

   ```bash
   aws cognito-idp admin-delete-user --user-pool-id "$POOL_ID" --username "github_<id>"
   ```

5. **Record what you disabled** (usernames + logins + date) in the remediation
   ticket. Gateway-side Postgres rows derived from these users are not removed by
   the Cognito action; if the deployment has tenant/org rows keyed to a disabled
   user, raise a follow-up for that cleanup.

## Verify the gate

- **Denied path:** sign in with a GitHub account outside `ALLOWED_ORGS`. Expect a
  redirect to `/login?error=not_authorized` and **no** new `github_*` user in the
  pool.
- **Allowed path:** sign in with an in-org account. Expect a successful session.
- **Cannot-verify path:** if the redirect carries `error=org_check_unavailable`,
  the allowlist could not be evaluated (missing/insufficient org token, GitHub
  error). This is deliberately distinct from `not_authorized` — investigate the
  token rather than the user. Broker CloudWatch logs name the failing org.

## Configuring the org-check token (recommended)

When `github_auth_token_secret_arn` is unset the broker falls back to the
signing-in user's own OAuth token. `read:org` is requested at `/start`, so this
works only while the OAuth App is org-approved; when it is not, GitHub answers
302/404 and every login fails with `org_check_unavailable`.

To make org checks robust, create a secret holding a GitHub token with `read:org`
and point the variable at it:

```bash
aws secretsmanager create-secret \
  --name adp/dev/gateway/github-org-token \
  --secret-string '{"token":"<github-token-with-read:org>"}'
```

Then set `github_auth_token_secret_arn` in
`environments/dev/modules/gateway.tfvars` and re-run `gateway-infra-apply.yml`.
The broker accepts either a raw string or a JSON object with a `token` key.

## Rollback

Revert the PR and re-apply the previous broker variables. The change is code +
Terraform variables only — no schema change, no new AWS resources — so rollback
restores the prior behaviour exactly. Note that reverting restores the
**open default**, so treat it as a temporary measure and set
`github_auth_allowlist_mode` explicitly instead where possible.
