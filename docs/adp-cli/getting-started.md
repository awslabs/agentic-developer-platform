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

The installer also downloads and verifies the skills archive into
`~/.adp/skills/`. Each setup command copies skills intended for that client into
`~/.codex/skills/` or `~/.claude/skills/` and marks each ADP-owned directory.
Re-running setup refreshes unchanged ADP skills, but leaves directories without
an ADP marker or with local changes untouched and prints their paths. If an
older deployment does not serve a skills archive, setup notes this and
continues without installing skills. The CLI may include no skills until a
deployment publishes them.

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

### macOS apps

The dashboard's **Apps & CLI Setup → macOS apps** section covers Claude App and
compatible Codex-based ChatGPT App builds. They can reuse your existing ADP
login, but desktop configuration depends on the installed app's capabilities.
See [macOS app setup](macos-apps.md) for connection steps, current support limits
and troubleshooting. Automatic ChatGPT desktop setup is not yet available.

### Claude Desktop

After signing in, install a credential helper tied to the selected ADP deployment:

```bash
adp claude-desktop setup
adp claude-desktop setup --check
```

The first command prints the proposed provider (ADP gateway), Anthropic Messages
base URL (`<your gateway>/api`), bearer authentication scheme, absolute helper
path (`~/.adp/bin/adp-claude-desktop-token`), and the endpoint listing models
available to your deployment and account. The helper uses ADP's existing login
and refreshes tokens as needed. Setup also resolves your tenant (`adp tenant
use TENANT_ID` first if your login can see more than one) and pins it in the
helper beside the deployment. A subsequent `adp deployment use` or `adp tenant
use` does not retarget it; rerun setup under a new selection to change its
target. Setup is
repeatable and does not modify Claude Desktop or Claude Code settings. Use
`--json` for `helper_path`, `base_url`, `deployment`, and `settings_written`
(`false`).

`--check` makes one authenticated request to the selected gateway's models
endpoint and prints only the HTTP status and number of entitled models. It does
not print a token; a failed login requires `adp login`. This checks ADP gateway
authentication, **not** whether a particular Claude Desktop build accepts a
custom gateway or invokes a credential helper. Anthropic has not publicly
documented a writable Desktop gateway settings format; apply the printed
values only if your Desktop build exposes compatible settings. Do not paste a
short-lived access token into Desktop settings. See the
[Desktop contract research](../research/claude-desktop-6850/desktop-contract.md)
for the public documentation checked and the remaining GUI uncertainty.

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

## Dev Box SSH prerequisites

The Settings → Dev Box page shows your box's SSH alias and these commands; it
never shows credentials. Each connection fetches a fresh 15-minute grant through
the CLI's `ProxyCommand`.

- OpenSSH with `ssh-keygen`.
- AWS CLI v2 and the Session Manager plugin, used only as transport for the
  grant the CLI receives from ADP. Do **not** run `aws configure` or supply
  personal AWS keys.
- A signed-in CLI (`adp login`, `adp status`) and a running box.

```bash
adp update
adp devbox ssh-config <box-name>   # writes ~/.ssh/config Host adp-devbox-<box-name>
adp devbox ssh <box-name>          # terminal session; verify the host-key fingerprint
```

VS Code Remote - SSH, Kiro IDE and Kiro Crew remote-host mode take the printed
alias. Details, host-key verification and named-deployment prefixes:
[Connect from your laptop to an ADP Dev Box](../kiro-crew/connect-from-laptop.md).

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
catalog. The update also refreshes the verified skills archive; re-run
`adp codex setup` or `adp claude setup` to copy updated skills into either
client. `adp doctor --checks skills` compares the archive version with the CLI
version and reports drift. `adp update --rollback` restores the previous local
executables and skills archive, leaving session data in place. The option
`--rollback --to VERSION` checks that the locally saved previous version matches.

`adp tools logout` removes only unchanged skills marked as ADP-owned from both
clients, preserving unmarked or locally modified skill directories. It also
unregisters ADP tool configuration and revokes an active session; use
`adp logout` instead to clear local session data without removing client skills.

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
