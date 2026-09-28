# Installation, sign-in and everyday use

[CLI guide](README.md) · [Command reference](command-reference.md)

## Prerequisites

- macOS or Linux with Bash, `curl`, `jq` and Python 3.9 or newer.
- The URL of an existing ADP deployment and an account on it.
- Codex or Claude Code installed and available on `PATH` to use that launcher.
- AWS CLI v2 and an authorized AWS role session only when creating AWS roles
  directly. Ordinary ADP browser login and tool use do not require local AWS
  credentials.

For example, if you manage the tools with npm:

```bash
npm install -g @openai/codex
npm install -g @anthropic-ai/claude-code
```

Use the Node.js version supported by your chosen tool. You only need the tool
you intend to run.

## Install from your ADP deployment

The installer is downloadable before authentication. Obtain its URL from your
deployment's sign-in page or administrator.

```bash
curl -fsSL https://adp.example.com/api/cli/install.sh -o /tmp/adp-install.sh
less /tmp/adp-install.sh
sh /tmp/adp-install.sh --gateway-url https://adp.example.com/api
```

The default installation directory is `~/.adp/bin`. The installer adds it to
your shell configuration. Open a new terminal, or apply the printed `PATH`
instruction, then run `adp version` and `adp help`.

For a custom directory without editing your shell configuration:

```bash
sh /tmp/adp-install.sh --gateway-url https://adp.example.com/api \
  --prefix "$HOME/bin/adp-cli" --no-path-edit
export PATH="$HOME/bin/adp-cli:$PATH"
```

Installation remembers the gateway URL. It does not authenticate you or install
the ADP platform. Platform deployment has its own
[deployment guide](../adp-platform-deployment/deploy-with-agent.md).

## Sign in through the browser

```bash
adp login
adp status
```

The CLI opens an approval page and displays a short code. Sign in to ADP in the
browser and approve the matching request. GitHub sign-in can be used when the
deployment's administrator has configured it.

On an EC2 instance, SSH session or machine without a browser:

```bash
adp login --no-browser
```

Open the printed approval URL on your own computer. You do not need to copy a
password or token into the EC2 terminal for this flow.

To explicitly configure a gateway on a single-deployment installation:

```bash
adp login --gateway-url https://adp.example.com/api
```

That installation has one shared session. Use a build with
[named deployments](environments.md) for simultaneous environments.

## Sign in as an administrator

Administrators with native Cognito accounts can enter their Cognito username
and password directly:

```bash
adp admin login
adp admin setup --org example-org
```

`admin login` checks that the account has platform-administrator privileges. It
supports password-change and SMS/authenticator MFA challenges. If further
enrollment is required, follow the browser action it reports and rerun login.

`admin setup` checks Bedrock and GitHub configuration and offers to resume
pending setup. ADP administrator privileges do not grant AWS role-creation or
GitHub organization-owner privileges. Use a download handoff or an existing
GitHub App when someone else controls those systems.

For automation, use `--credentials-file /private/admin.json` or
`--credentials-stdin`; see [protected inputs](scripting-and-troubleshooting.md#protected-inputs).

## Set up and run your tools

```bash
adp codex setup
adp claude setup
adp codex
# Or, when you want Claude Code:
adp claude
```

Setup merges ADP settings into `~/.codex/config.toml` or
`~/.claude/settings.json`, preserving unrelated settings. It uses your existing
ADP session. Setting up a second tool does not require another login.

Arguments after a launcher go to that tool:

```bash
adp codex exec --skip-git-repo-check "Explain this directory"
adp claude --print "Explain this directory"
```

These commands can invoke a model and incur usage charges. Model availability,
permissions, budgets and Bedrock destinations are controlled by the selected
ADP deployment. Use `adp bedrock status` to inspect the configured destination.

`adp codex` starts its local authentication proxy when needed. After Claude
setup, bare `claude` also works. Prefer the ADP launchers when choosing among
named environments, because they pin the tool to the selected deployment.

For an always-running proxy on macOS, `adp daemon install` enables a launchd
service so bare `codex` can work; `adp daemon uninstall` removes that service.
The daemon commands are not supported on Linux. `adp serve` runs the proxy in
the foreground and stops with Ctrl-C.

### Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research)
uses the same local authentication proxy as Codex. Install Hermes with support
for `hermes config set` and `hermes config get --json --raw`, then configure it:

```bash
adp hermes setup
adp hermes
```

Setup uses Hermes' own configuration writer, preserving unrelated settings;
ADP does not require PyYAML in the system Python. It stores environment references
for the endpoint and credential. Each `adp hermes` launch supplies the selected
deployment's verified proxy endpoint through `ADP_HERMES_BASE_URL` and its local
credential through `ADP_GATEWAY_DUMMY`. It reuses that deployment's Codex proxy
when available, or starts one. Changing deployments needs no setup rewrite:

```bash
adp hermes --oneshot "Explain this directory"   # single prompt
adp hermes --tui                                # full TUI
adp --deployment dev hermes                     # select the ADP deployment
```

Always launch with `adp hermes`, including when an ADP daemon is running.
The launcher rejects stale transport settings and provider/profile overrides.
For a separate Hermes home, set `HERMES_HOME` to the same directory for both
setup and launch. Re-run `adp hermes setup` to migrate an older configuration
that contains a literal proxy port.

Hermes needs `git` to install; on Amazon Linux 2023 also install `libatomic`
for its bundled Node.js runtime. Model requests through the ADP proxy's
`/v1/chat/completions` route are budgeted, rate-limited and logged against your
ADP identity. Setup selects `sonnet45`; use `--model` or `model.default` in the
Hermes configuration to choose another model exposed by your ADP deployment.

## Refresh, log out and update

```bash
adp status
adp refresh
adp logout
adp update
adp version
```

Refresh normally happens automatically. `status` reports the local session and
expiry; it is not a model call or a live permissions check. If refresh fails
because the refresh session expired, run `adp login` again.

`adp update` downloads from the configured gateway. `adp update --to 1.0.0`
requires that exact version to be served; it is not a historical release
catalog. `adp update --rollback` restores the previous local executable copies,
leaving session data in place. `--rollback --to VERSION` checks that the locally
saved previous version matches.

To uninstall, use the downloaded installer and the same prefix used to install:

```bash
sh /tmp/adp-install.sh --uninstall
# Custom-prefix installation:
sh /tmp/adp-install.sh --uninstall --prefix "$HOME/bin/adp-cli"
```

Uninstall removes CLI files, not your session. Run `adp logout` first if you
also want to clear it, and uninstall an installed daemon before removing the
CLI. There is no `adp uninstall` command.

## Refresh-token import fallback

When browser approval is unavailable, the compatibility import flow can seed
the session from a refresh token supplied by the ADP browser setup flow:

```bash
adp import --gateway-url https://adp.example.com/api
```

Paste the token at the hidden prompt. Import discovers Cognito settings where
possible; explicit `--client-id`, `--user-pool-id` and `--region` are also
supported. Use a refresh token issued for the matching Cognito client. The
implementation also accepts `--refresh-token`, but prefer the prompt or stdin
to avoid storing credentials in shell history. The import fallback uses AWS CLI;
the primary `adp login --no-browser` flow does not require it.
