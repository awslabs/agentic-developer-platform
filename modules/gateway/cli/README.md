# Bedrock Gateway CLI Tools

CLI tools for authenticating with the Bedrock Gateway and configuring Claude Code.

## Contents

| File | Description |
|------|-------------|
| `bg-cognito-auth.sh` | Cognito authentication helper (login, import, refresh, token) |
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
| `Refresh token invalid or expired` (on `import`) | Copied token is stale or from another deployment | Sign in again and re-copy from Settings → Connect CLI |
| `Could not determine Cognito client_id` (on `import`) | Gateway discovery unreachable | Check `<gateway_url>/.well-known/cognito-config`, or pass `--client-id` + `--region` |
| `401 missing_token` | Claude Code not sending auth header | Check `apiKeyHelper` path in settings.json |
| `401 invalid_token` | JWT expired or wrong audience | Run `bg-cognito-auth.sh refresh` |
| `503 auth_not_configured` | Gateway can't reach Cognito | Check gateway pod logs |

## Security

- Tokens stored in `~/.bedrock-gateway/` with `600` permissions
- `bg-cognito-auth.sh token` outputs only the JWT to stdout (logs go to stderr)
- No credentials are logged or stored in plaintext
- M2M client secrets live in AWS Secrets Manager, not in code
