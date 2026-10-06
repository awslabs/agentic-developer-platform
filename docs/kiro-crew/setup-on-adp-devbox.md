# Setting up Kiro Crew on an ADP dev box

Run [Kiro Crew](https://github.com/kirodotdev/KiroCrew) on an ADP dev box so that
**every model call goes through the ADP gateway under your own ADP identity** —
budgeted, rate-limited and logged exactly like your Codex and Claude Code
traffic — with no model API key on the machine.

Verified 2026-10-05 on a fresh `adp-dev-box` CloudFormation clone (Ubuntu 24.04,
m7i.large) with Kiro Crew 0.7.2, `claude-agent-acp` 0.86.0, Claude Code 2.1.289
and the adp CLI. Wall time ≈ 15 minutes, most of it the installer.

## How it fits together

```
kirocrew chat · dashboard · Slack/Discord · cron · subagents
   └─ Kiro Crew gateway (systemd unit, loopback :5476)      agent.acp_backend = "claude"
        └─ claude-agent-acp (ACP harness, npm)
             └─ Claude Code CLI                               reads ~/.claude/settings.json
                  ├─ apiKeyHelper = `adp token`               (written by `adp claude setup`)
                  └─ Bedrock-mode requests → https://<gateway>/api → ADP gateway → Bedrock
```

Kiro Crew's default agent backend is `kiro-cli`; you do **not** install it. You
select the `claude` harness instead, which delegates every model turn to the
Claude Code CLI — the tool `adp claude setup` already knows how to wire to the
gateway. No credential is stored in Kiro Crew's config or environment: the
harness asks `adp token` for a short-lived token on every request.

## Prerequisites

| Requirement | On an ADP dev box |
|---|---|
| Linux x86_64 with `sudo` | yes (Ubuntu 24.04) |
| Python ≥ 3.12, Node ≥ 22, npm | installed by the dev-box bootstrap (the Kiro Crew installer brings its own Python if yours is older) |
| adp CLI (`~/.adp/bin/adp`) and Claude Code (`claude`) | installed by the dev-box bootstrap |
| An ADP account that can see the deployment | you |
| Port 5476 free on loopback | `ss -ltn \| grep 5476` prints nothing |

## 1. Sign in to ADP

```bash
adp login --no-browser        # prints a code and a URL; approve it in your browser
adp tenant list               # only if the CLI has the tenant verb and you belong to several tenants …
adp tenant use aws-e          # … pick the tenant your usage is billed to
adp status                    # expect: Access token: valid
```

If `adp token` says *"Select one visible tenant with --tenant, ADP_TENANT or
adp tenant use TENANT_ID"*, you skipped `adp tenant use`; the Claude harness fails
with the same message until it is set. Older adp CLIs without the `tenant` verb
do not need this step.

## 2. Wire Claude Code to the gateway and prove it

```bash
adp claude setup
claude --print --model us.anthropic.claude-sonnet-5 "Say hello in five words"
```

`adp claude setup` merges into `~/.claude/settings.json`: `apiKeyHelper`
(= `adp token`), `CLAUDE_CODE_USE_BEDROCK=1`, `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`,
`ANTHROPIC_BEDROCK_BASE_URL=<gateway>/api` and `AWS_REGION`. The smoke test must
print a reply; fix this first, because Kiro Crew inherits exactly this setup.

Model ids in this mode are the **Bedrock-style ids** your deployment exposes —
`us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-5-5`,
`us.anthropic.claude-haiku-4-5-20251001`, … Short aliases such as `sonnet45`
are not valid here: Claude Code hangs on an unknown id until it times out.

## 3. Install Kiro Crew and the Claude harness

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh      # signed wheel → ~/.kiro/crew-venv, launcher ~/.local/bin/kirocrew
npm install -g @agentclientprotocol/claude-agent-acp        # the ACP harness
kirocrew --version && claude-agent-acp --version
```

The installer verifies the release manifest signature and the wheel's SHA-256,
and installs dependencies from prebuilt wheels only (no compiler needed).

## 4. Select the claude harness

```bash
kirocrew setup --agent-only                                    # writes ~/.kiro/agents/kirocrew.json; ignore the kiro-cli hint
kirocrew config set agent.acp_backend claude                   # chat and worker sessions
kirocrew config set agent.member_acp_backend claude            # crew-member DM threads (default is the kiro harness)
kirocrew config set agent.model us.anthropic.claude-sonnet-5   # default model; "auto" resolves to the head of the gateway list (Opus)
```

Config lives in `~/.kiro/crew/config.json`.

## 5. Run it as a service

On Ubuntu 23.10+ the agent sandbox needs an AppArmor `userns` profile; the
systemd unit installs and attaches it, so run the gateway as a service rather
than in a terminal:

```bash
sudo env PATH="$PATH" HOME="$HOME" SUDO_USER="$USER" "$HOME/.local/bin/kirocrew" service install
```

This writes `/etc/systemd/system/kirocrew.service` (`User=<you>`, loopback
:5476, `EnvironmentFile=/etc/kirocrew/kirocrew.env`), installs
`/etc/apparmor.d/kirocrew-userns` and attaches it to the launcher the unit
executes. Plain `sudo kirocrew` fails because root's `PATH` lacks `~/.local/bin`
— hence the full path.

Give the harness its routing environment (no secrets), then restart:

```bash
sudo tee /etc/kirocrew/kirocrew.env >/dev/null <<EOF
CLAUDE_CODE_EXECUTABLE=$HOME/.local/bin/claude
CLAUDE_AGENT_ACP_BIN=$HOME/.local/bin/claude-agent-acp
ANTHROPIC_MODEL=us.anthropic.claude-sonnet-5
CLAUDE_CODE_SUBAGENT_MODEL=us.anthropic.claude-haiku-4-5-20251001
EOF
sudo systemctl restart kirocrew
curl -s http://127.0.0.1:5476/api/health       # {"ok": true, "app": "kirocrew", "version": "0.7.2"}
kirocrew doctor | grep -E 'claude-acp|gateway'   # claude-acp ✅ … (Claude Code installed)
```

The dashboard listens on loopback only. From a laptop:

```bash
ssh -NL 5476:localhost:5476 <dev-box>     # in one terminal
kirocrew token                             # on the box: prints a one-time http://localhost:5476?token=… URL
```

## 6. Use it

```bash
kirocrew chat -m "In one sentence, what is this machine's hostname? Use the hostname command."
kirocrew chat --model us.anthropic.claude-haiku-4-5-20251001 -m "Which model are you? Answer with the id only."
kirocrew chat                                          # interactive
kirocrew run TASK.md                                   # autonomous task from a spec file
kirocrew spawn run 'summarise the last 10 commits in ~/adp'   # background subagent
```

Every turn appears in the ADP usage log under your user
(`/api/usage/logs?org_id=<org>`), and the gateway's budgets and rate limits
apply. Two log lines are normal on every session and can be ignored:
`mcp_ref_guard … unresolved=@kirocrew-computer` (desktop automation is not
installed) and `claude-acp … stderr: [session/create] phase=…` timing.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `apiKeyHelper failed: exited 4: Select one visible tenant` | run `adp tenant use <org>` as the service user |
| `claude --print` hangs for ~3 minutes | model alias not valid in Bedrock mode — use a `us.anthropic.…` id |
| HTTP 429 `rate_limited`, `limit_type=concurrent` | your ADP user/org concurrent cap (default 10); an ADP admin raises it under Rate Limits |
| `sudo kirocrew: command not found` | use the full launcher path as in step 5 |
| `kirocrew doctor`: `kiro-cli not found (the default agent backend)` | expected — the claude harness replaces it |
| `cgroup v2 scope enforcement unavailable` at service start | the host does not delegate controllers to the user slice; the sandbox still applies, only fork-bomb/memory ceilings are not enforced |
| The reply names a model you did not pick | `agent.model` was `auto`; pin it (step 4) or use `--model` |

## Removing it

```bash
sudo "$HOME/.local/bin/kirocrew" service uninstall    # stops and removes the unit and the AppArmor profile
rm -rf ~/.kiro/crew ~/.kiro/crew-venv ~/.local/bin/kirocrew
npm uninstall -g @agentclientprotocol/claude-agent-acp
```

Claude Code's ADP wiring from `adp claude setup` is unaffected.

## What was verified

| Check | Result |
|---|---|
| `kirocrew chat -m "Reply with exactly: KIROCREW-ADP-OK"` | `KIROCREW-ADP-OK` |
| Natural prompt using a shell tool | answered with the hostname and the serving model |
| `agent.model` pin | reply identifies as `us.anthropic.claude-sonnet-5` |
| `--model` per-call override | reply identifies as the Haiku id |
| ADP usage ledger | the call is recorded under the user's id with tokens and cost |
| Service | `User=ubuntu`, loopback :5476, AppArmor profile attached, `/api/health` ok |
