# Bring Your Own GitHub App

How to connect a GitHub App you created yourself (or recovered from a failed
registration) to an ADP deployment, so that **agents can operate on your
repos** and **users can sign in with GitHub**.

ADP normally creates this App for you via the manifest flow (Settings →
Connections → *Set up GitHub App*), which bakes in every setting below
automatically. Use this guide when that flow isn't an option: you already have
an App, your org requires Apps to be created by a specific admin process, or a
registration attempt failed after the App was created on GitHub (the App
exists but ADP never captured its credentials).

> **One App does both jobs.** Since #2607, ADP uses a single GitHub App for
> the agent pipeline (webhooks, tokens, check runs) **and** for "Sign in with
> GitHub" (the App's *user authorization* / OAuth feature). There is no
> separate OAuth App. If you bring your own App, configure both halves or
> login will not work even though agents do.

The authoritative source for everything on this page is the manifest builder
in `modules/gateway/src/admin/connections/service.py::_build_app_manifest` —
if this doc and the code disagree, the code wins.

---

## 1. Values you need from your deployment

Every URL below is deployment-specific. Collect these first (your platform
operator has them; they are also in SSM / the deploy-instance issue):

| Placeholder | What it is | Where it comes from |
|---|---|---|
| `<WEBHOOK_URL>` | Webhook ingress endpoint, e.g. `https://<api-id>.execute-api.<region>.amazonaws.com/<env>/github` | SSM `WEBHOOK_URL` param published by the webhook-ingress apply |
| `<DASHBOARD_URL>` | CloudFront domain of the admin dashboard, e.g. `https://dxxxxxxxxxxxx.cloudfront.net` | SSM `/adp/<env>/gateway/cloudfront-domain` |
| `<AUTH_BROKER_URL>` | GitHub auth-broker endpoint, e.g. `https://<api-id>.execute-api.<region>.amazonaws.com/<env>/auth/github` | Gateway API GW (`/auth/github/{proxy+}` route) |

## 2. Create the App (or open your existing one)

Create at **your org** → Settings → Developer settings → GitHub Apps → New
GitHub App (or open the existing App's settings page). App names are globally
unique — prefix with your org name, e.g. `<your-org>-adp-agent-platform`.

### 2.1 Basic settings

| Field | Value |
|---|---|
| Homepage URL | anything (e.g. `https://github.com/apps/<app-slug>`) |
| Webhook → Active | ✅ checked |
| Webhook URL | `<WEBHOOK_URL>` |
| Webhook secret | generate one (`openssl rand -hex 32`) and keep it — ADP needs the same value (§4) |
| Setup URL | `<DASHBOARD_URL>/api/admin/connections/github/install-callback` |
| "Redirect on update" | ✅ checked (GitHub calls this *Setup on update*) |

Without the Setup URL + redirect-on-update, GitHub leaves the browser on
github.com after an install and the installation never attaches to an ADP
tenant (bug #2823). This is the single most commonly missed setting.

### 2.2 Repository permissions (for agents to operate on repos)

| Permission | Access | Why |
|---|---|---|
| Contents | **Read and write** | agents clone, branch, commit, push |
| Issues | **Read and write** | agents read task issues, comment progress, open child issues |
| Pull requests | **Read and write** | agents open/update PRs, post review comments |
| Checks | **Read and write** | agents publish check runs (pass/fail gates) |
| Metadata | **Read** | mandatory baseline (GitHub forces it) |

**Recommended addition — Organization permissions → Members: Read.** The
manifest flow doesn't request it (agents don't need it), but the login path's
tenant matcher calls the org-membership API to map a signing-in user to your
org's tenant. Without it, GitHub answers that call with a 302, the platform
logs `check_org_membership: 302 … app likely lacks 'Organization members:
read'`, membership verification returns false, and users may not auto-match
to your org tenant (an admin can still approve them manually). No other
organization or account permissions are required.

### 2.3 Event subscriptions

Subscribe to exactly these events:

- `issues`
- `issue_comment` ← this is the one that triggers agents (`@agent-<persona>` mentions)
- `pull_request`
- `pull_request_review`
- `pull_request_review_comment`
- `label`

### 2.4 User authorization — the OAuth half (for "Sign in with GitHub")

On the same App settings page:

| Field | Value |
|---|---|
| Callback URL | `<AUTH_BROKER_URL>/callback` |
| "Request user authorization (OAuth) during installation" | ❌ **unchecked** |
| "Expire user authorization tokens" | leave default |

Notes:

- OAuth **scopes are not configured on the App** — the auth broker requests
  `user:email read:org` at runtime.
- The login flow needs a **second credential pair**: the App's **Client ID**
  (shown at the top of the settings page) and a **client secret** (click
  *Generate a new client secret*). These are different from the App ID +
  private key used by the agent pipeline. Keep both pairs.

### 2.5 Visibility: private vs public

| | Private App | Public App |
|---|---|---|
| Who can complete the sign-in page | **only the App's owner org members with access** — everyone else gets GitHub's 404 | any GitHub user |
| Who can install it | owner org only | any org/user with the link |
| ADP platform access control | unchanged — still gated by ADP onboarding approval | unchanged |

If anyone outside the owner org should log in to the dashboard, the App must
be **public** (App settings → Advanced → *Make public*). Making it public
does **not** open your platform: new users still land in a pending access
request that a platform admin approves, and agent triggering is governed by
the per-tenant trigger policy (#3134). A private App is fine for a
single-org deployment where every user belongs to the owner org.

### 2.6 Generate the signing key

App settings → *Private keys* → **Generate a private key**. This downloads a
`.pem` file — this plus the **App ID** (top of the settings page) is what the
agent pipeline uses to mint installation tokens.

## 3. Install the App

App settings → *Install App* → pick your org → **Only select repositories** →
choose every repo agents should work on. You can add repos later from
GitHub → Org settings → GitHub Apps → Configure.

An installed-but-uncovered repo is the most common "agents don't trigger"
cause: mentions in a repo the installation doesn't cover never reach ADP.

## 4. Hand the credentials to ADP

You now hold four secrets:

1. **App ID** (number)
2. **Private key** (`.pem`) — agent pipeline
3. **Client ID + client secret** — login (OAuth)
4. **Webhook secret** — delivery signature verification

### Preferred: the Connections UI (once #3354 ships)

Dashboard → Settings → Connections → **Connect an existing App** → paste App
ID, private key, webhook secret, and the OAuth client ID + secret. ADP
validates the App ID/key pair against `GET /app`, checks the App's webhook
URL, permissions, and events against this deployment's expectations (warns on
mismatch — see §2.2/§2.3), and stores everything in the right places.

### Until then: manual seeding (platform operator, per environment)

```bash
ENV=dev  # your environment

# 1+2 — agent pipeline credentials (per-tenant secret; <tenant> = your ADP org slug)
aws secretsmanager put-secret-value \
  --secret-id "adp/${ENV}/tenants/<tenant>/github-app" \
  --secret-string "{\"app_id\":\"<APP_ID>\",\"private_key\":\"$(cat app.pem | awk 1 ORS='\\n')\"}"

# 3 — login (OAuth) credentials read by the auth broker
aws secretsmanager put-secret-value \
  --secret-id "adp/${ENV}/cognito/github-oauth-credentials" \
  --secret-string '{"client_id":"<CLIENT_ID>","client_secret":"<CLIENT_SECRET>"}'

# 4 — webhook signature secret read by the ingress Lambda
aws secretsmanager put-secret-value \
  --secret-id "adp/${ENV}/webhook-ingress/github-webhook-secret" \
  --secret-string '<WEBHOOK_SECRET>'
```

## 5. Verify

Run these in order; each isolates one half of the integration.

**Agent half:**

```bash
# unsigned probe must be rejected (proves the ingress is up and verifying)
curl -s -o /dev/null -w "%{http_code}\n" -X POST "<WEBHOOK_URL>"   # expect 401
```

Then post a comment containing `@agent-developer` on an issue in a covered
repo. Within ~1 minute the App should reply "Agent started"; within ~5 you
should have a PR. If nothing happens, check GitHub → App settings → Advanced
→ Recent Deliveries: `403 invalid signature` means webhook-secret mismatch
(§4 item 4); no delivery at all means the repo isn't covered by the
installation (§3) or the webhook URL is wrong (§2.1).

**Login half:**

Open `<DASHBOARD_URL>`, click *Log in with GitHub*, complete the GitHub
authorize page, and confirm you land back on the dashboard authenticated (a
brand-new user lands on a pending-approval page — that's correct; a platform
admin approves the request). A GitHub 404 on the authorize page means the App
is private and you're not an owner-org member (§2.5). Bouncing back to the
ADP login page with no session means the broker callback URL doesn't match
§2.4.

## 6. Quick reference — what breaks when something's missing

| Missing/wrong setting | Symptom |
|---|---|
| Webhook URL wrong | mentions do nothing; App's Recent Deliveries show a different target or errors |
| Webhook secret mismatch | every delivery `401 invalid signature` |
| `issue_comment` event not subscribed | mentions do nothing; no delivery attempted |
| Contents/PR permission missing | agent starts but fails to push / open PR |
| Checks permission missing | agent works but no check runs appear |
| Setup URL missing | install "succeeds" on GitHub but never appears in ADP Connections (#2823) |
| OAuth callback URL wrong/missing | GitHub login bounces to the ADP login page with no session |
| Client ID/secret not seeded | login button errors or Cognito rejects the identity |
| App private + outside user | GitHub 404 on the sign-in authorize page |
| Repo not in installation | agents work in covered repos, silent no-op in this one |
| Org "Members: Read" permission missing | login works but users don't auto-match to the org tenant; gateway logs `check_org_membership: 302` |

## Related

- Issue #3354 — "Connect an existing App" import UI (this doc is its companion)
- Issue #2985 — multiple Apps per deployment (deferred; today one App per deployment)
- `docs/design-notes/2951-github-org-to-adp-tenant.md` — tenant model the install flow attaches into
- `modules/agent-factory/webhook-ingress/README.md` — webhook ingress internals
