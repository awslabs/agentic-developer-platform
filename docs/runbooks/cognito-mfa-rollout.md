# Cognito MFA & Threat Protection — Staged Rollout

**Subsystem:** Gateway authentication (`modules/gateway/infra/modules/cognito/`)

**Issue:** #5666 (A11), parent #5677

## What changed in source, and why a rollout is needed

Two source changes land together, and only one of them is safe to apply without
sequencing.

1. **`mfa_configuration` is now effectively `ON`.** The cognito module has
   defaulted to `ON` since #133, but the root module passed
   `var.cognito_mfa_configuration`, which defaulted to `OPTIONAL`. Terraform
   resolves the caller's value, so the module's hardened default was dead code
   and the composed source default was `OPTIONAL`. This does not establish any
   deployed pool configuration. Both layers now default to
   `ON` and `OFF` is rejected by validation at both.
2. **Threat protection (`user_pool_add_ons`) now exists** and is reachable as
   `cognito_threat_protection_mode`. It defaults to `OFF` and is inert until an
   operator opts in.

Change 2 is cost-gated but behaviour-neutral at its default. **Change 1 is the one
that needs ordering**: applying `mfa_configuration = "ON"` to a pool whose users
have never enrolled a second factor changes what happens at their next sign-in.
Read the next section before applying to an environment with real users.

Applying Terraform, changing a pool, and enrolling users are live operations.
This runbook is not evidence that any of them were performed.

## The failure mode this ordering exists to prevent

With `mfa_configuration = "ON"` and software-token MFA enabled, Cognito requires
every user to have a second factor. Behaviour depends on how the user signs in:

- **GitHub broker sign-in (`/auth/github`, the primary path)** is subject to
  pool MFA. The broker uses `ADMIN_USER_PASSWORD_AUTH` against a Cognito user;
  this is not native Cognito federation. Its current provisioner expects
  `AuthenticationResult` and does not complete `MFA_SETUP` or
  `SOFTWARE_TOKEN_MFA` challenges. Enforcing `ON` can therefore break GitHub login.
- **Hosted-UI email/password sign-in** enters TOTP association at next sign-in
  (`MFA_SETUP` challenge) for a user with no enrolled factor. This completes in the
  hosted UI without operator action, but it is a visible change and users need to
  have an authenticator app available.
- **Automation using `USER_PASSWORD_AUTH` or `ADMIN_USER_PASSWORD_AUTH`** receives
  an MFA challenge instead of tokens. A script that expects
  `AuthenticationResult` in the first response will break. This is the
  highest-risk category because it fails in CI, not in a browser.

Identify all password-flow consumers, including the GitHub broker, before applying. The known `USER_PASSWORD_AUTH` consumers of
the SPA client at the time of writing are `platform/evals/lib/cognito.sh` (shared
by the budget-ratelimit, cli-onboarding and bedrock-routing evals),
`platform/scripts/bedrock-routing-validate.sh`, and the `BG_COGNITO_PUBLIC_AUTH=1`
branch of `modules/gateway/cli/bg-cognito-auth.sh`. Re-derive the list for your
environment rather than trusting this one:

```bash
grep -rn "USER_PASSWORD_AUTH" --include=*.sh --include=*.py --include=*.yml . \
  | grep -v ADMIN_USER_PASSWORD_AUTH
```

Machine identities that must not face an interactive challenge belong on the
`agent` client (client-credentials) and require a supported migration before enforcement. Do not assume a per-user
password-flow exemption exists.

## Ordered rollout

| Order | Action | Required outcome |
|---|---|---|
| 0 | Build and deploy a gateway image containing the reviewed source fixes | Verify the image digest; a ConfigMap update or recycling the old image does not install the fix |
| 1 | Confirm the live pool configuration and explicitly stage `cognito_mfa_configuration = "OPTIONAL"` where the current pool is OPTIONAL | Record a reviewed staging exception beside the tfvars assignment as described below; do not downgrade an ON pool by assumption |
| 2 | Implement supported broker challenge handling or a supported alternative auth flow, migrate automation, and prepare independent operator recovery | Validate new and existing GitHub and hosted-UI users and recovery before enforcement; enrollment alone does not repair the broker |
| 3 | Only after step 2 succeeds, remove the `OPTIONAL` pin and apply `ON` | Verify supported sign-in flows complete MFA and token issuance; otherwise keep the documented staging exception |
| 4 | Optionally set `cognito_threat_protection_mode = "AUDIT"` | Risk is scored and logged; **no** sign-in outcome changes |
| 5 | Optionally promote to `ENFORCED` after reviewing audit findings | Risky sign-ins are challenged or blocked |

An OPTIONAL exception must have a preceding comment in the same tfvars file:
`# cognito-mfa-staging: <tracking issue URL>; owner=<owner>; exit=<broker compatibility and recovery evidence>`.
Review that exception with the environment change. The source defaults remain ON;
OFF is invalid. Do not enforce ON on the current broker before step 2 is complete.

Steps 4 and 5 are **not free**. Threat protection requires the Cognito **Plus**
feature plan, billed **per monthly active user**. Confirm the cost for your
user count before step 4; that is why the default is `OFF` and why
`user_pool_tier` is left unmanaged (`null`) at `OFF` rather than pinned, since
pinning a tier is itself a billing change.

## Verification

Check the **effective** pool configuration, not the tfvars:

```bash
aws cognito-idp describe-user-pool --user-pool-id <pool-id> \
  --query 'UserPool.{mfa:MfaConfiguration,tier:UserPoolTier,addons:UserPoolAddOns}' \
  --profile <profile> --region <region>
```

Confirm a real sign-in still works on **both** paths before declaring the step
complete — federated (GitHub) and hosted-UI — plus the broker challenge handling and any password-flow automation
you identified above. These live checks belong to the separately authorized rollout. Source-side assertions are covered by
`modules/gateway/tests/infra/test_cognito_auth_hardening.py`, which pins the
un-shadowed defaults and the add-on wiring; those tests are not evidence about a
deployed pool.

## Recovery

- **A user cannot complete MFA (lost device).** Reset that user's factor:
  `aws cognito-idp admin-set-user-mfa-preference --user-pool-id <pool-id> --username <user> --software-token-mfa-settings Enabled=false,PreferredMfa=false`,
  then have them re-enroll at next sign-in. This is per-user and does not weaken
  the pool.
- **Automation is broken by enforcement.** Re-pin `cognito_mfa_configuration =
  "OPTIONAL"` in that environment's tfvars and apply. Prefer this bounded,
  recorded revert over `OFF`, which validation now rejects outright.
- **Total inability to sign in.** GitHub is not an independent recovery path
  while the broker uses the same Cognito password flow. Before enforcement,
  establish and verify an operator AWS IAM session independent of gateway/Cognito
  login, with the ability to inspect the pool and execute the reviewed rollback.
  Record the operator, session recovery procedure, and rollback evidence in the
  rollout record. Keep OPTIONAL staging until this prerequisite is satisfied.
- **Threat protection is challenging legitimate users.** Set
  `cognito_threat_protection_mode = "AUDIT"` to keep visibility without
  enforcement, or `OFF` to remove the add-on.

## Remainders not closed by the source change

- `ALLOW_USER_PASSWORD_AUTH` is **retained** on the SPA client. The SPA itself does
  not use it (hosted-UI authorization-code + PKCE only), but the eval harnesses
  above authenticate from pods with no AWS credentials and therefore cannot use the
  SigV4-signed admin flow. Retiring it requires giving the clean-room evals a
  credential path and deciding the fate of `BG_COGNITO_PUBLIC_AUTH`; MFA `ON`
  already blunts the flow, since a password alone no longer completes a sign-in.
- Existing users are **not** retroactively enrolled by any step here. Enforcement
  prompts them at next interactive sign-in; there is no backfill.
