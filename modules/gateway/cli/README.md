# Bedrock Gateway CLI Tools

CLI tools for authenticating with the Bedrock Gateway and configuring Claude Code.

## Contents

| File | Description |
|------|-------------|
| `bg-cognito-auth.sh` | Cognito authentication helper (login, refresh, token) |
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
# Login (interactive, one-time)
bg-cognito-auth.sh login --gateway-url https://gateway.example.com/api

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
| `401 missing_token` | Claude Code not sending auth header | Check `apiKeyHelper` path in settings.json |
| `401 invalid_token` | JWT expired or wrong audience | Run `bg-cognito-auth.sh refresh` |
| `503 auth_not_configured` | Gateway can't reach Cognito | Check gateway pod logs |

## Security

- Tokens stored in `~/.bedrock-gateway/` with `600` permissions
- `bg-cognito-auth.sh token` outputs only the JWT to stdout (logs go to stderr)
- No credentials are logged or stored in plaintext
- M2M client secrets live in AWS Secrets Manager, not in code
