# Bedrock Gateway CLI Tools

CLI tools for authenticating with the Bedrock Gateway and configuring Claude Code.

## Contents

| File | Description |
|------|-------------|
| `bg-cognito-auth.sh` | Cognito authentication helper (login, import, refresh, token, serve) |
| `bg-gateway-proxy.py` | Localhost auth proxy started by `serve` — zero-touch auth for Codex (stdlib python3, no pip installs) |
| `bg-auth.sh` | Legacy SigV4 credential exchange (deprecated) |
| `install.sh` | Installation script |
| `examples/claude-settings-bedrock-gateway.json` | Claude Code settings (Bedrock format via gateway) |
| `examples/claude-settings-cognito.json` | Claude Code settings (Anthropic format via gateway) |

## Quick Start (New Machine)

### Prerequisites

- `curl`, `jq`, `aws` CLI v2
- A Cognito user account (ask your platform admin)
- Claude Code installed (`npm install -g @anthropic-ai/claude-code`)

### Step 1: Install the auth script

```bash
cp cli/bg-cognito-auth.sh ~/bin/
chmod +x ~/bin/bg-cognito-auth.sh
```

> **Note:** `install.sh` installs only `bg-auth.sh` (the legacy SigV4 helper). `bg-cognito-auth.sh` must be copied manually, as above.

### Step 2: Configure Claude Code

```bash
mkdir -p ~/.claude
cp cli/examples/claude-settings-bedrock-gateway.json ~/.claude/settings.json
```

Edit `~/.claude/settings.json` and replace `<CLOUDFRONT_DOMAIN>` with your gateway domain.

### Step 3: Login (one-time)

```bash
~/bin/bg-cognito-auth.sh login \
  --gateway-url https://<CLOUDFRONT_DOMAIN>/api \
  --user-pool-id <USER_POOL_ID> \
  --client-id <CLIENT_ID> \
  --region us-east-1
```

It will prompt for username and password. Tokens are saved to `~/.bedrock-gateway/`.

### Step 4: Launch Claude Code

```bash
claude
```

That's it. Claude Code calls `bg-cognito-auth.sh token` automatically via `apiKeyHelper`, which returns a fresh Cognito JWT. The token auto-refreshes — you won't need to login again for 30 days.

## Signed in with GitHub? Use `import` instead of `login`

If you signed in to the gateway dashboard with GitHub, you have **no Cognito password** — your account was provisioned with a random one you never see. `login` cannot work for you. Instead, seed the CLI from the session the browser already established:

1. Sign in to the dashboard with GitHub.
2. Open **Settings → Connect CLI**, click **Reveal token**, and copy the refresh token.
3. On your laptop:

```bash
~/bin/bg-cognito-auth.sh import --gateway-url https://<CLOUDFRONT_DOMAIN>/api
```

It prompts for the refresh token with hidden input — paste it there. Then configure `apiKeyHelper` exactly as in Step 2 above and run `claude`.

```
Browser: Sign in with GitHub
    └─ broker mints id/access/refresh tokens against the public Cognito client
         └─ SPA stores the refresh token (sessionStorage, this tab only)
              └─ you copy it into `bg-cognito-auth.sh import`
                   ├─ helper discovers client_id + region from
                   │    <gateway_url>/.well-known/cognito-config
                   ├─ validates the token with one REFRESH_TOKEN_AUTH call
                   │    (nothing is written unless this succeeds)
                   └─ writes ~/.bedrock-gateway/{config,tokens}.json (0600)
                        └─ `token` auto-refreshes from then on — no password
```

### Options

| Flag | Required | Notes |
|------|----------|-------|
| `--gateway-url <url>` | yes | Used for discovery and stored in `config.json` |
| `--refresh-token <token>` | no | For scripting only. **Prefer stdin** — an argv flag lands in shell history and `ps` output |
| `--client-id <id>` | no | Overrides discovery |
| `--user-pool-id <id>` | no | Overrides discovery |
| `--region <region>` | no | Overrides discovery (default `us-east-1`) |

There is no `--identity-pool-id`: `import` performs no AWS-credential exchange and writes nothing to `~/.aws/`. The `token` subcommand — the only thing Claude Code calls — needs just `client_id`, `region`, and a valid refresh token.

### Notes and limits

- **The refresh token is a long-lived credential.** Treat it like a password: never paste it into a shared terminal, a chat, or a URL.
- **Nothing is written on failure.** `import` validates the token with Cognito before touching `config.json` or `tokens.json`, so a bad paste cannot break a working session.
- **The token is per-browser-tab.** The dashboard holds it in `sessionStorage`; close the tab and you must sign in again to get a new one.
- If **Connect CLI** says to sign out and back in, your session has no refresh token — re-authenticate to get one.
- Piping works for automation: `printf '%s' "$TOKEN" | bg-cognito-auth.sh import --gateway-url https://<CLOUDFRONT_DOMAIN>/api`.

## Using Codex: zero-touch auth with `serve`

Claude Code re-asks this helper for a token whenever it needs one (`apiKeyHelper`).
**Codex has no such hook** — it reads its credential from an env var once at
launch and never asks again. So `export ADP_GATEWAY_TOKEN=$(bg-cognito-auth.sh token)`
works for about an hour, and then every request 401s until you restart Codex.

`serve` closes that gap. It runs a small proxy on localhost that injects a
freshly-refreshed token into every request, so you authenticate once and never
touch tokens again — including across a session that runs for days.

### Step 1: Install both files

```bash
cp cli/bg-cognito-auth.sh cli/bg-gateway-proxy.py ~/bin/
chmod +x ~/bin/bg-cognito-auth.sh
```

`bg-gateway-proxy.py` must sit **next to** `bg-cognito-auth.sh` — `serve` looks
for its sibling. It needs only stdlib `python3`, which macOS and Linux both ship.

### Step 2: Authenticate once

```bash
~/bin/bg-cognito-auth.sh import --gateway-url https://<CLOUDFRONT_DOMAIN>/api
# ...or `login` if you have a Cognito password
```

### Step 3: Configure Codex

Add to `~/.codex/config.toml` (the helper deliberately does **not** write this
file for you — it is yours):

```toml
model = "openai.gpt-5.6-sol"
model_provider = "adp-gateway"

[model_providers.adp-gateway]
name = "ADP Gateway (local auth proxy)"
base_url = "http://127.0.0.1:9191/openai/v1"
wire_api = "responses"
# Codex requires env_key to name an existing env var but never validates its
# value — the proxy discards whatever arrives and injects the real token.
env_key = "ADP_GATEWAY_DUMMY"
```

> **Model switching inside Codex just works.** The in-app `/model` picker
> writes short slugs (`gpt-5.6-sol`) into this file, but the gateway serves
> models under their prefixed ids (`openai.gpt-5.6-sol`). The proxy adds the
> missing `openai.` prefix on the way through, so either spelling is fine —
> the model just has to be one the gateway actually serves.

### Step 4: Run the proxy, then Codex

```bash
~/bin/bg-cognito-auth.sh serve          # foreground; Ctrl-C to stop
```

In another terminal:

```bash
ADP_GATEWAY_DUMMY=unused codex
```

That's it. Leave the proxy running as long as you like — token refresh happens
per request, behind the scenes.

### How it works

```
codex  ──POST http://127.0.0.1:9191/openai/v1/responses
   │
   └─ bg-gateway-proxy.py (loopback only)
        ├─ calls `bg-cognito-auth.sh token`  ← the ONE refresh implementation
        │    └─ reuses the cached JWT, or renews ~5 min before the 60-min expiry
        ├─ drops any client Authorization / x-api-key
        ├─ sets Authorization: Bearer <fresh token>
        ├─ prefixes bare model names with `openai.` on /openai/* requests
        │    (the in-app /model picker writes short slugs)
        └─ forwards to <gateway_url> and streams the response back verbatim
             (SSE chunks unbuffered — Codex sends stream=true)
```

This is the same shape as the hosted-agent sigv4-proxy sidecar
(`modules/agent-factory/agent-worker-image/`, Codex → `127.0.0.1:9090`): local
listener, per-request auth injection, streaming passthrough. Only the auth
material differs — Cognito JWTs here, SigV4 there.

### Options

| Flag | Default | Notes |
|------|---------|-------|
| `--port <port>` | `9191` | Must match the port in your `config.toml` `base_url` |
| `--foreground` | (always) | Accepted for explicitness; daemonization is a non-goal — use `&`, `tmux`, or a second terminal |

### Security properties

- **Loopback only.** The proxy binds `127.0.0.1` and there is no flag to widen
  it. A listener that injects your credential must never be reachable from the
  LAN, so the bind address is a hardcoded literal, enforced by a test.
- **No secrets in output.** One line per request (method, path, status) on
  stderr — never the token, never headers, never bodies, never query strings.
- **One refresh implementation.** The proxy shells out to `bg-cognito-auth.sh
  token`; the Cognito logic is not duplicated in Python. Concurrent requests are
  serialized so two refreshes can't race on `tokens.json`.
- **Proxy vs. gateway errors are distinguishable.** A failure inside the proxy
  returns `502` with `{"error": "proxy_token_error" | "proxy_upstream_error"}`;
  anything else is the gateway's own status and body, passed through unchanged.

### Troubleshooting `serve`

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Not configured` | No `~/.bedrock-gateway/config.json` | Run `import` (GitHub login) or `login` first |
| `Proxy script not found` | `bg-gateway-proxy.py` not beside `bg-cognito-auth.sh` | Copy both files to the same directory |
| `A gateway proxy is already running (pid N)` | A proxy from a previous session is live | `kill N`, then re-run `serve` |
| `502 proxy_token_error` | Refresh token expired (30 days) or Cognito rejected it | `bg-cognito-auth.sh status`, then `import`/`login` again |
| `502 proxy_upstream_error` | Gateway unreachable from your machine | Check the `gateway_url` in `config.json` and your network |
| Codex hangs with no output | `base_url` port ≠ `--port` | Make them match (default `9191`) |
| Codex: connection refused | Proxy not running | Start `serve` in another terminal |

## How It Works

```
Developer runs `claude`
    │
    ├─ Claude Code calls apiKeyHelper: bg-cognito-auth.sh token
    │   └─ Returns cached Cognito JWT (auto-refreshes if near expiry)
    │
    ├─ Claude Code sends request to gateway
    │   URL: https://<CLOUDFRONT_DOMAIN>/api/bedrock/invoke-with-response-stream
    │   Auth: JWT in x-api-key header
    │
    ├─ CloudFront → strips /api prefix → ALB → EKS pods
    │
    ├─ Gateway validates JWT against Cognito JWKS
    │   Extracts: org_id, team_id, role, account_type
    │
    └─ Gateway proxies to Amazon Bedrock
        Returns response to Claude Code
```

## Auth Commands

```bash
# Login (interactive, one-time — requires a Cognito password)
bg-cognito-auth.sh login --gateway-url https://gateway.example.com/api

# Seed from a GitHub browser login (no password; token pasted on stdin)
bg-cognito-auth.sh import --gateway-url https://gateway.example.com/api

# Refresh tokens (non-interactive)
bg-cognito-auth.sh refresh

# Get current access token (used by apiKeyHelper)
bg-cognito-auth.sh token

# Run the localhost auth proxy for Codex (zero-touch; see the Codex section above)
bg-cognito-auth.sh serve --port 9191

# Check auth status
bg-cognito-auth.sh status

# Logout (clear tokens)
bg-cognito-auth.sh logout
```

## Settings File Options

Two formats are supported depending on how Claude Code talks to the gateway:

### Option A: Bedrock format (recommended)

Uses `ANTHROPIC_BEDROCK_BASE_URL`. Claude Code sends Bedrock-format requests to `/bedrock/invoke-with-response-stream`.

```json
{
  "env": {
    "AWS_REGION": "us-east-1",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    "ANTHROPIC_BEDROCK_BASE_URL": "https://<CLOUDFRONT_DOMAIN>/api"
  },
  "apiKeyHelper": "bash ~/bin/bg-cognito-auth.sh token",
  "apiKeyHelperTtlMs": 3300000,
  "model": "global.anthropic.claude-opus-4-6-v1"
}
```

### Option B: Anthropic API format

Uses `ANTHROPIC_BASE_URL`. Claude Code sends standard Anthropic Messages API requests to `/v1/messages`.

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://<CLOUDFRONT_DOMAIN>/api"
  },
  "apiKeyHelper": "bash ~/bin/bg-cognito-auth.sh token",
  "apiKeyHelperTtlMs": 3300000,
  "model": "global.anthropic.claude-opus-4-6-v1"
}
```

## M2M / Agent Authentication

For automated agents (GitHub Actions, EKS workloads), use the Cognito `client_credentials` flow instead of username/password.

Agent credentials are stored in AWS Secrets Manager (`bedrockgw-dev-agent-cognito-credentials`). The flow:

```bash
# 1. Fetch credentials from Secrets Manager
CREDS=$(aws secretsmanager get-secret-value \
  --secret-id bedrockgw-dev-agent-cognito-credentials \
  --query SecretString --output text)

# 2. Get M2M token from Cognito
TOKEN=$(curl -s -X POST "$TOKEN_ENDPOINT" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=$ID&client_secret=$SECRET&scope=bedrockgw/invoke" \
  | jq -r '.access_token')

# 3. Use as ANTHROPIC_API_KEY
export ANTHROPIC_BASE_URL="https://<CLOUDFRONT_DOMAIN>/api"
export ANTHROPIC_API_KEY="$TOKEN"
```

See `.github/workflows/gateway-agent-test.yml` for a complete working example.

## Token Refresh

- Access tokens expire in 60 minutes
- `bg-cognito-auth.sh token` auto-refreshes 5 minutes before expiry
- Refresh tokens last 30 days
- `apiKeyHelperTtlMs: 3300000` (55 min) ensures Claude Code calls the helper before expiry

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Not logged in` | No saved tokens | Run `bg-cognito-auth.sh login` |
| `Token expired` | Refresh token expired (30 days) | Run `bg-cognito-auth.sh login` again |
| `Token refresh failed` | Cognito user disabled or password changed | Re-login |
## Authenticating when the user pool is behind a WAF

If the deployment protects its Cognito user pool with an AWS WAF web ACL — an IP
allowlist, typically because access is fronted by a ZTNA product — `login` and
`refresh` use the **admin** auth flow (`admin-initiate-auth`,
`ADMIN_USER_PASSWORD_AUTH`) rather than the public one.

The reason is not cosmetic. A web ACL on a user pool covers the pool's *public*
API operations as well as the hosted UI, and those are served from
`cognito-idp.<region>.amazonaws.com`. That is an AWS-owned hostname, so it cannot
be published through a corporate tunnel: the request leaves your machine directly
and arrives from your own address, which the allowlist does not contain.
`initiate-auth` then fails `ForbiddenException`. SigV4-signed `Admin*` operations
are outside that surface, so they keep working.

This is automatic whenever a `user_pool_id` is present in
`~/.bedrock-gateway/config.json`. It requires:

- `cognito-idp:AdminInitiateAuth` (and `AdminRespondToAuthChallenge` for a first
  login) on your IAM identity
- `ALLOW_ADMIN_USER_PASSWORD_AUTH` in the app client's `explicit_auth_flows`

To force the public flow — a deployment whose users have no admin IAM and whose
pool has no web ACL:

```bash
BG_COGNITO_PUBLIC_AUTH=1 ./bg-cognito-auth.sh login --gateway-url https://<DOMAIN>/api
```

`<CLOUDFRONT_DOMAIN>` throughout this document means whatever hostname serves the
dashboard. If the deployment has a custom domain, use that rather than the
distribution's default name — a WAF or ZTNA policy is usually written against the
custom hostname, and the default `*.cloudfront.net` name may be retired.

| `Refresh token invalid or expired` (on `import`) | Copied token is stale or from another deployment | Sign in again and re-copy from Settings → Connect CLI |
| `Could not determine Cognito client_id` (on `import`) | Gateway discovery unreachable | Check `<gateway_url>/.well-known/cognito-config`, or pass `--client-id` + `--region` |
| `401 missing_token` | Claude Code not sending auth header | Check `apiKeyHelper` path in settings.json |
| `ForbiddenException` from Cognito | A WAF web ACL on the user pool refused the request | Unset `BG_COGNITO_PUBLIC_AUTH` so the admin flow is used — see above |
| `AccessDeniedException` on `AdminInitiateAuth` | Your IAM identity lacks the admin Cognito permission | Grant it, or set `BG_COGNITO_PUBLIC_AUTH=1` if the pool has no web ACL |
| `401 invalid_token` | JWT expired or wrong audience | Run `bg-cognito-auth.sh refresh` |
| `503 auth_not_configured` | Gateway can't reach Cognito | Check gateway pod logs |

## Security

- Tokens stored in `~/.bedrock-gateway/` with `600` permissions
- `bg-cognito-auth.sh token` outputs only the JWT to stdout (logs go to stderr)
- No credentials are logged or stored in plaintext
- M2M client secrets live in AWS Secrets Manager, not in code
