# Bedrock Gateway CLI Tools

For the user guide and complete command reference, start at
**[docs/adp-cli/](../../../docs/adp-cli/README.md)**. It covers installation,
every command group, administrator and user workflows, scripting, and the
availability of environment switching. The details below also cover the
underlying compatibility scripts.

CLI tools for authenticating with the Bedrock Gateway and configuring Claude Code
or Codex.

## Contents

| File | Description |
|------|-------------|
| `adp` | **The CLI you run.** Thin wrapper: `login`, `status`, `codex setup`, `claude setup`, `update` |
| `install.sh` | Installer for `adp` — one line, run via `curl … \| sh` from your gateway |
| `bg-cognito-auth.sh` | Cognito authentication core (login, import, refresh, token, serve). `adp` delegates every auth verb to it |
| `bg-gateway-proxy.py` | Localhost auth proxy started by `serve` — zero-touch auth for Codex (stdlib python3, no pip installs) |
| `adp_common.py` | Shared transport, session reuse, output envelope and exit codes. Every `adp-*.py` helper imports it |
| `adp-admin.py` | Administrator login and resumable guided setup (`adp admin login`, `adp admin setup`) |
| `adp_deployments.py` | Named deployments and the one selection rule, shared by the bash and python halves (stdlib Python 3) |
| `adp-bedrock.py` | Bedrock account connection and routing, used by `adp admin bedrock connect` (stdlib Python 3) |
| `adp-aws.py` | Personal AWS account connection — `adp aws` ([guide](aws.md)) |
| `adp-github.py` | Connect a repository you have access to — `adp github` ([guide](github.md)) |
| `adp-github-admin.py` | GitHub App registration and status for administrators — `adp admin github` ([guide](github-admin.md)) |
| `adp-superplane.py` | Workspaces, GPU deployments, cloud accounts and provider credentials — `adp superplane` ([guide](../../../docs/adp-cli/superplane.md)) |
| `adp-superplane-onboarding.py` | Workspace and provider onboarding — `adp superplane onboarding`: capability and readiness reporting, plan review, credential-reference binding, durable operation receipts |
| `adp-task.py`, `adp_task_client.py` | Submit, monitor and abort work as a registered Task principal — `adp task` ([guide](../../../docs/adp-cli/tasks.md)) |
| `adp-flow.py` | Follow and control AI-DLC delivery flows — `adp flow` ([guide](flow.md)) |
| `adp-doctor.py` | What this deployment offers you, and why a call failed — `adp capabilities`, `adp doctor` ([guide](doctor.md)). Read-only |
| `command-manifest.json` | The checked list of commands, their mutation class and required capability. Held against the real dispatcher, install/update lists, download allowlist and server contract by `tests/cli/test_command_manifest.py` |
| `bg-auth.sh` | Legacy SigV4 credential exchange (deprecated) |
| `examples/claude-settings-bedrock-gateway.json` | Claude Code settings (Bedrock format via gateway) |
| `examples/claude-settings-cognito.json` | Claude Code settings (Anthropic format via gateway) |

Superplane workspace and deployment creates keep their private operation receipts
after success, so an identical invocation reconciles the same resource even when
the earlier command's output was lost. Failed or deleted operations retain their
receipts and block identical creates. See the [Superplane guide](../../../docs/adp-cli/superplane.md)
before starting a separate create with a different name.

## Quick Start (New Machine)

### Prerequisites

- `curl`, `jq`, `python3`
- `ps` (the `procps` package) — only for named deployments, and only on a slim
  Linux image that ships without it. It is already present on macOS and on any
  normal Linux install, and `/proc` is used in preference where available.
- A Cognito user account (ask your platform admin) — GitHub sign-in counts
- Claude Code (`npm install -g @anthropic-ai/claude-code`) or the Codex CLI

The `aws` CLI is **not** required. Refresh is routed through the gateway, so
ordinary users need no AWS credentials of their own.

### Start to finish

```bash
curl -fsSL https://<CLOUDFRONT_DOMAIN>/api/cli/install.sh | sh -s -- \
    --gateway-url https://<CLOUDFRONT_DOMAIN>/api
adp login          # approve once in the browser
adp status         # confirm you are signed in
adp claude setup   # or: adp codex setup
claude             # or: adp codex
```

The installer puts `adp` and its helper siblings — the files listed in Contents
above, which is `CLI_FILES` in `install.sh` — side by
side in `~/.adp/bin` (override with `--prefix`), adds that directory to your PATH,
and remembers the gateway URL in `~/.bedrock-gateway/config.json` — which is why
no later command needs a flag. `adp update` re-pulls from the same gateway;
`adp update --rollback` undoes it. `sh install.sh --uninstall` removes the files
and leaves your session alone.

`adp status --json` reports the selected deployment, gateway, selection source
and local session metadata in the shared JSON envelope. It never refreshes or
contacts the gateway: `configured` means a cached session exists, including an
expired access token that can refresh on use; `unavailable` exits 1 when no
session exists. Credential values are excluded. The `aws_profile` field identifies
the AWS profile shared by every alias of the selected deployment. Named profiles
use `adp-deployment-<stable-id>`; older `bedrock-gateway-<name>` profiles are retired
on login, refresh or logout, so scripts should use the reported profile name.

Reinstalling against a different gateway refuses before changing binaries or
session files. Use `adp deployment add <name> --url <gateway>` to add that gateway.

**One login is shared by every tool.** `adp login` seeds `~/.bedrock-gateway/`
once; both `setup` verbs only write config and never authenticate, so adding a
second tool costs one command and no second sign-in.

The `setup` verbs **merge** into `~/.claude/settings.json` and
`~/.codex/config.toml` — your existing permissions, hooks, MCP servers and other
providers survive — and re-running them changes nothing.

> Prefer to read what you run? `curl -fsSL https://<CLOUDFRONT_DOMAIN>/api/cli/install.sh -o install.sh`,
> read it, then `sh install.sh --gateway-url https://<CLOUDFRONT_DOMAIN>/api`.

## Several deployments at once

One installed CLI serves any number of ADP deployments — development,
integration, pre-production — each with its own login, its own tool config and
its own agent sessions. Register them once:

```bash
adp deployment add dev         --url https://<dev-host>
adp deployment add integration --url https://<integration-host>
adp deployment add preprod     --url https://<preprod-host>
adp deployment list            # which are registered, and which one is selected
```

Then give each terminal its own target and sign in there:

```bash
export ADP_DEPLOYMENT=integration   # this terminal, for as long as it lives
adp login
adp codex                           # or: adp claude
```

Which deployment a command uses, **first match winning**:

| Selection | Scope |
|---|---|
| `adp --deployment <name> <verb> …` | this one command (before the verb) |
| `ADP_DEPLOYMENT=<name>` | this terminal |
| `adp deployment use <name>` | the saved default, for new terminals |

An unknown name **fails**; it is never quietly swapped for another deployment.
`adp deployment use` changes only the saved default — terminals that named their
own deployment are unaffected, and so is anything already running. `adp logout`
signs out of the selected deployment only.

`adp deployment add` registers locally and makes no request, so a deployment can
be registered long before you sign in to it. `adp deployment remove` forgets one
locally: it never touches the cloud, and it refuses to remove the saved default
or a deployment with a running command, proxy, or installed daemon. Removing the
last alias deletes that deployment's private local session and state. The original
legacy store is retained.

`adp --deployment dev claude setup` pins bare `claude` to dev, including its token
helper. Changing the saved default does not change that setup. Use
`adp --deployment integration claude` to launch against integration with temporary
settings; the saved Claude configuration stays intact. Custom tool arguments are
forwarded, but overrides of ADP's endpoint or authentication settings are refused.

Named `codex setup` reserves a stable local proxy port. On macOS, run
`adp --deployment dev daemon install` for bare Codex; each deployment has a separate
daemon pinned to its own session and port. Setup determines which deployment bare
Codex uses. Use `adp --deployment <name> codex` for simultaneous terminals.

Each deployment keeps its session, state, logs and Codex proxy under
`~/.adp/deployments/<id>/`, and gets its own AWS profile
(`bedrock-gateway-<name>`) so three deployments do not overwrite each other's
credentials. Two names for the same URL are one deployment under two labels — one
session, not two. Do not set `ADP_PROXY_PORT` when running concurrent sessions: it
pins a single port.

If you already had a single-deployment setup, it keeps working untouched and
appears in `adp deployment list` as `default`. Nothing is moved and you do not
need to sign in again.

> **Rolling back past this release.** `adp update --rollback` restores the
> previous CLI executables and deliberately leaves your deployments and sessions
> alone. An `adp` from before named-deployment support has no `deployment` verb,
> so while rolled back it uses the original single-deployment store and ignores
> the registry. Your deployments are not deleted — they reappear unchanged when
> you `adp update` forward again.

Design and rationale: [multiple deployments design
note](../../../docs/design-notes/5413-cli-multiple-deployments.md).

## Connect an AWS account for Bedrock

After `adp update` and `adp login`, one command creates the role, verifies it and
assigns the organization's routing rule:

```bash
adp admin bedrock connect --account 123456789012 --org example-org --profile aws-admin
```

The role name is generated automatically. Add `--team Engineering` to route one
team, or `--user developer@example.com` to route one person. Organization and team
names are resolved through ADP; ambiguous names require an exact ID. User rules
take priority over team rules, then organization rules. The command asks once
before provisioning and assigning, including when it replaces an existing rule.

If an AWS administrator needs to create the role, download the same template and
parameters used by the UI:

```bash
adp admin bedrock connect --account 123456789012 --org example-org --download ./example-role
```

Give `template.yaml`, `parameters.json` and `README.md` to the AWS administrator.
Keep the directory, including `destination.json`. Once the role is created:

```bash
adp admin bedrock connect --resume ./example-role
```

Resume verifies the saved account and role and applies the saved organization,
team or user rule. Download and resume require no local AWS credentials.
The directory is private (0700), with files readable only by their owner (0600).
The parameters include the destination's ExternalId; share them privately with
the AWS administrator. ADP tokens and AWS credentials are never included.

For scripts, append `--yes --json`. Use `--dry-run` first to inspect the resolved
account and scope without creating a destination, role or rule. `adp admin bedrock list`
shows existing destinations. All diagnostics go to stderr; `--json` keeps stdout
machine-readable.

The command uses the existing ADP login and platform-admin API checks. Direct
provisioning additionally requires AWS CLI v2 and local AWS credentials with
CloudFormation/IAM role-creation permissions. `--profile` is optional if the
current AWS credential chain already points at the account. STS checks the actual
account before provisioning; a mismatch stops the command. Neither the AWS profile
nor its credentials are sent to ADP. CloudFormation parameters are passed through
temporary private files, never command-line arguments or logs.

Rerunning the same command reuses the pending destination and existing stack.
It never replaces a failed stack automatically. Verification must pass before a
rule is assigned; failed setup leaves the destination available for a retry.
Downloaded setup uses the same gateway when resumed. This feature requires the
gateway's destination-setup API and the updated CLI download endpoint.

For effective routing, run `adp bedrock status`. Administrators can use
`adp admin bedrock verify DESTINATION_ID` or `status --user USER`. See the
[model access guide](bedrock.md) for scope, handoff, scripting and regression checks.

## Follow and control delivery flows: `adp flow`

Follow and control AI-DLC delivery flows from the terminal, reusing the session
`adp login` already established:

```bash
adp flow list                    # your flows, worst news first
adp flow show FLOW_ID            # progress, blockers, gates, next eligible work
adp flow watch FLOW_ID           # follow it; Ctrl-C detaches, it does NOT cancel
adp flow plans FLOW_ID           # plan versions, and which revision is proposed
adp flow draft preview --help    # preview a revision of an existing unapproved draft
adp flow draft save --help       # save the reviewed revision without approving execution
adp flow gate approve GATE_ID --expect-plan-hash HASH
```

Readable output by default, `--json` on stdout with diagnostics on stderr.
Exiting a watch **detaches** and leaves hosted execution untouched. Approvals
prompt, refuse rather than assume an answer without a terminal, and can be bound
to the exact plan revision you read. Cost stays three-valued: unmeasured spend is
reported as `unknown`, never as `$0.00`.

`adp flow start --repo OWNER/NAME --issue NUMBER` opens hosted intent refinement
and, in a terminal, continues to a bounded plan preview. `--resume SESSION_ID`
returns to the same conversation. `create --file plan.json` reviews a prepared
engine plan. Noninteractive acceptance requires both `--yes` and
`--expect-plan-hash HASH`. See the [delivery flow guide](flow.md) for supported
commands, deployment prerequisites and the remaining full-inception limitations.

## Using the scripts directly (no `adp`)

The rest of this document covers the underlying scripts directly. Everything below
still works — `adp` wraps it rather than replacing it — and is what to read if you
want the details, are debugging, or maintain a hand-installed setup.

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

### Step 3: Sign in (one-time, browser approval — no password, no copy-paste)

```bash
~/bin/bg-cognito-auth.sh login --web --gateway-url https://<CLOUDFRONT_DOMAIN>/api
```

Your browser opens the dashboard's approval page showing the same short code as
your terminal — click **Approve** and you're signed in. Tokens are saved to
`~/.bedrock-gateway/`, minted on a **CLI-specific app client**: the on-disk
refresh token is short-lived (24 h by default, vs 30 days for the browser) and
**rotates on every background refresh**, so a stolen copy dies the next time
your machine refreshes.

Fallbacks:
- **Cognito password account** (not created via GitHub sign-in): use `login`
  without `--web` — it prompts for username/password.
- **Headless machine** (SSH, no browser): use `import` — see the next section.
- `--no-browser` prints the approval URL instead of opening a browser.

### Step 4: Launch Claude Code

```bash
claude
```

That's it. Claude Code calls `bg-cognito-auth.sh token` automatically via `apiKeyHelper`, which returns a fresh Cognito JWT. The token auto-refreshes — you won't need to login again for 30 days.

## Headless machine? Use `import` instead of `login --web`

`login --web` needs a browser on the same machine. On a box that has none (SSH
target, container), seed the CLI from a browser session on another machine.
This also remains the fallback while a deployment hasn't enabled web CLI login
yet (`login --web` reports it). Note the pasted refresh token is the **SPA
client's** (30-day, non-rotating) — prefer `login --web` wherever a browser
exists:

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
# Codex sends this variable's value as its bearer token. The proxy requires it to
# be the local capability the proxy published (see "Who may use the proxy" below),
# then discards it and injects the real Cognito token — so it authenticates Codex
# to the proxy and never reaches the gateway.
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

In another terminal, pass the capability the proxy just published:

```bash
ADP_GATEWAY_DUMMY="$(jq -r .capability ~/.bedrock-gateway/proxy.json)" codex
```

That's it. Leave the proxy running as long as you like — token refresh happens
per request, behind the scenes.

> **Why not `ADP_GATEWAY_DUMMY=unused` any more?** The proxy now requires a
> capability rather than a placeholder, because any website you visit can send
> requests to `127.0.0.1` and would otherwise be able to spend your token (see
> below). Each `serve` publishes a new capability, so read it from the file rather
> than hardcoding it. `adp codex` does this for you.

> **With `adp` installed this is one command: `adp codex`.** It health-checks the
> proxy, starts it in the background if needed, sets `ADP_GATEWAY_DUMMY` itself
> and hands you into Codex — one terminal, no prefix to remember. The two steps
> above are what it automates, and remain the path for a hand-installed setup with
> no `adp`. To make the bare `codex` command work, `adp daemon install` keeps the
> proxy always-on (macOS) and provisions a mode-0600 capability that remains valid
> across launchd restarts; `adp daemon uninstall` reverts it. Claude Code needs
> none of this — `apiKeyHelper` refreshes per request, so bare `claude` works and
> `adp claude` is only a fail-fast login check.

### How it works

```
codex  ──POST http://127.0.0.1:9191/openai/v1/responses
   │
   └─ bg-gateway-proxy.py (loopback only)
        ├─ refuses the caller unless it presents the published capability,
        │    and refuses anything carrying browser markers (Origin /
        │    Sec-Fetch-Site) or a non-loopback Host  ← all before any token
        │    is fetched, so a refused request never reaches the gateway
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
- **Entitlement is proven, not assumed** (Issue #5686). Loopback decides which
  *machines* can connect; it does not decide which *callers* may spend your
  token. Any other process on your machine can reach `127.0.0.1`, and so can
  **any website you visit** — browsers allow a page to `fetch()` your loopback
  address. Such a page never sees your token, but it doesn't need to: it can make
  the proxy spend it and read the reply. So every relayed request must present the
  capability the proxy published, and requests carrying browser markers (`Origin`,
  including the `null` origin sent by sandboxed pages, or a cross-site
  `Sec-Fetch-Site`) are refused outright. CORS preflights are answered locally
  with `403` and no `Access-Control-Allow-*` header.
- **DNS rebinding is refused.** An attacker domain can be made to resolve to
  `127.0.0.1`, so the packet arrives on loopback legitimately. The proxy checks
  the `Host` you asked for, not just the address it arrived on, and accepts only
  loopback names.
- **The capability never travels where it could leak.** It is minted per proxy
  process, held in memory, published only into the mode-`0600`
  `~/.bedrock-gateway/proxy.json`, and passed to Codex via an environment variable
  — never on a command line (`ps` is world-readable), never in a URL, and never in
  the log. It is dropped before the upstream call, so the gateway never sees it.
  The local identity route `/_adp/proxy` answers without it (a launcher must be
  able to ask an *other* deployment's proxy whose it is) and never discloses it.
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
| `403 proxy_unauthorized` | `ADP_GATEWAY_DUMMY` is unset, still the old `unused` placeholder, or from a previous proxy process | Use `adp codex`, or re-export it: `ADP_GATEWAY_DUMMY="$(jq -r .capability ~/.bedrock-gateway/proxy.json)"`. A stale `export ADP_GATEWAY_DUMMY=unused` in your `~/.zshrc` or `~/.bash_profile` is the usual cause |
| `403 proxy_forbidden_origin` | Something sent an `Origin`/`Sec-Fetch-Site` header — usually a browser, i.e. the abuse this blocks | Expected. Drive the proxy from a CLI tool, not a web page or a browser tab |
| `403 proxy_forbidden_host` | The request's `Host` was not a loopback name (a DNS-rebinding signal) | Point your client at `127.0.0.1`, not a hostname that resolves there |
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
# Sign in via browser approval (primary path — no password, no copy-paste)
bg-cognito-auth.sh login --web --gateway-url https://gateway.example.com/api

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

- `login --web` tokens ride the CLI app client: refresh tokens last 24 hours
  (deployment-configurable) and ROTATE — each refresh returns a new refresh
  token and invalidates the old one. The helper already persists the rotated
  token; just don't copy `tokens.json` between machines (the copy dies on the
  original's next refresh).
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


## Administrator onboarding

Install from the command on the deployment's sign-in page; downloads require no
login. The installer prints an absolute path you can run before reloading PATH.

```sh
adp admin login          # Cognito username/password, password change and MFA
adp admin setup          # check existing setup and resume missing providers
adp admin setup --dry-run --json
```

On a fresh deployment, `adp admin setup` offers Cognito login before GitHub is
configured. Ordinary developers continue using `adp login` and their existing
`adp codex` / `adp claude` commands. Admin login requires no local AWS credentials.

Automation can supply a private mode-0600 JSON file via `--credentials-file`, or
JSON from a secret manager via `--credentials-stdin`; never put passwords or MFA
codes in arguments. Keys: `username`, `password`, and challenge inputs
`new_password`, `sms_mfa_code`, `software_token_mfa_code` when required. Unsupported
MFA enrollment remains pending and must be completed in the browser.

Setup reports each provider as verified, configured, pending, failed or
unavailable. Providers not yet shipped remain unavailable. `--json` produces one
object; exit codes are 0 success, 1 usage, 2 authentication, 3 authorization,
4 external action pending, and 5 failure. Domain commands ship separately.

### Bootstrap release smoke test

`modules/gateway/scripts/test-cli-bootstrap.py` compares a fresh public download
with the expected checkout before accepting credentials. It checks an explicit
Cognito pool/client binding, installs into a temporary home, signs in through
`adp admin login`, refreshes the saved session, and checks administrator setup.
Use an existing test administrator and a private 0600 credentials JSON file with
`username`, `password`, and challenge inputs when required. No AWS credentials
are needed by this test.

```sh
python modules/gateway/scripts/test-cli-bootstrap.py \
  --gateway-url https://DEPLOYMENT/api \
  --expected-pool POOL_ID --expected-client CLI_CLIENT_ID \
  --expected-cli-dir modules/gateway/cli \
  --credentials-file /private/test-admin.json \
  --report /private/bootstrap-report.json
```

Use `--artifacts-only` instead of `--credentials-file` to check deployment before
signing in. A mismatch fails without sending credentials. Reports contain only
binding metadata, artifact hashes and step outcomes. This deployed check remains
separate from mocked authentication tests and must pass after the gateway and its
pool-scoped Cognito IAM policy are released.

Before deployment, the opt-in component check
`tests/auth/test_cli_native_cognito_live.py` can reuse the running #5173 fixture
identities with actual Cognito and deployed refresh. Set `ADP_NATIVE_LIVE_CONFIG`
to the private environment config, `ADP_NATIVE_LIVE_STATE` to its `state.json`,
and `ADP_NATIVE_LIVE_CLIENT` to the **CLI** app client ID (not the discovery
`client_id`, which belongs to the browser). Run with `pytest -q --tb=no` to keep
raw SDK failures out of output. It creates no users or grants and changes no
passwords. Its native routes/database run locally; deployment, gateway IAM and
PostgreSQL rate-limit concurrency still require release verification.

Usage and redacted inference metadata: [Usage CLI](../../../docs/adp-cli/usage.md).
Human run discovery, transcripts, explanation streaming and supported controls: [Agent Activity CLI](../../../docs/adp-cli/agent.md). Task submission continues to use `adp task`.

`adp superplane research` reads findings/sources/stats and reviews proposals through the domain API. See `docs/adp-cli/research.md` for revision-bound decisions and unavailable scan/generation boundaries.

Authorized tenant selection and concurrent terminal isolation: [Tenant CLI](../../../docs/adp-cli/tenant.md).
