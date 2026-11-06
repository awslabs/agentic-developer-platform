# macOS apps with ADP

Open **Apps & CLI Setup → macOS apps** in your ADP dashboard for the Claude App
and ChatGPT App guides. The page remains at `/setup` and uses the current
deployment's gateway URL. The **CLI tools** tab retains the CLI setup guides.

Supported desktop apps reuse your existing ADP login. Install the ADP CLI and
sign in using [Getting started](getting-started.md) if you have not already:

```bash
adp login
adp status
```

Select the intended [deployment](environments.md) and [tenant](tenant.md)
before configuring an app. Model access and budgets come from that selection.
The ADP CLI installer does not install Claude App or ChatGPT App.

## Claude App

Your macOS Claude App build must expose custom Anthropic Messages gateway and
credential-helper settings. Claude Code configuration does not configure the
desktop app. Confirm compatibility before applying connection details; the
[Desktop contract research](../research/claude-desktop-6850/desktop-contract.md)
records the limits of the publicly documented interface.

Prepare the connection:

```bash
adp claude-desktop setup
```

This installs a credential helper using your existing ADP login and prints the
gateway base URL, absolute helper path and entitled-models endpoint. It does
not write Claude App settings. In a compatible app build, configure the
Anthropic Messages provider and bearer authentication through the printed
helper. Use the printed base URL without adding a `/v1` suffix.

Keep the helper configured so tokens refresh; do not copy access tokens into
the app. The helper is pinned to the deployment and tenant selected at setup.
Changing the CLI's default selection does not retarget the helper. Rerun setup
under the new selection to change the app's connection.

Check gateway authentication and model discovery:

```bash
adp claude-desktop setup --check
```

This prints an HTTP status and model count. It does not verify that Claude App
accepts the configuration or invokes the helper. Open a new app conversation,
send a short message and confirm that request appears in ADP's Log Viewer.
If your build lacks these settings, use a compatible build supplied by your
platform administrator or the existing Claude Code setup.

## ChatGPT App

This guide applies to compatible Codex-based ChatGPT builds that support custom
model providers and read `~/.codex/config.toml`. The app's display name alone
does not establish support for that configuration. Confirm the installed build
before changing settings.

**Automatic ChatGPT desktop setup is not yet provided by the ADP CLI.** A
platform administrator must configure the supported build's desktop
connection. Existing CLI commands prepare parts of that connection:

| Existing command | What it provides | What remains for the desktop app |
|---|---|---|
| `adp codex setup` | Codex provider configuration | A supported desktop authentication mechanism |
| `adp codex` | Starts or reuses the proxy and supplies authentication to the CLI process | Authentication when the desktop app opens from Finder |
| `adp daemon install` | A macOS service that keeps the local proxy running | Supplying credentials to the desktop app and verifying its provider address |

For an app using the local proxy, its provider address must match the live
proxy, and its authentication must use the real local capability. Despite its
name, `ADP_GATEWAY_DUMMY` carries that capability; a value such as `unused` or
`dummy` is rejected. A terminal environment export does not configure a Finder
launch. A one-time macOS environment change also does not establish setup that
survives logout or reboot.

Use the app build's supported credential mechanism and preserve existing
settings. Compatible Codex versions support
[command-backed credential refresh](https://developers.openai.com/codex/config-advanced/);
confirm support in the installed desktop engine before configuring it. Do not
publish a copied access token or a temporary local port as permanent setup.

After configuring the connection, fully quit and reopen the app. Send a short
message and confirm it in ADP's Log Viewer. Repeat after restarting the Mac and
after credential renewal before treating desktop setup as complete. The
[Codex CLI flow](getting-started.md#set-up-and-run-your-tools) remains available
when desktop configuration is unavailable.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Missing environment variable: ADP_GATEWAY_DUMMY` | The app has a provider configuration but did not receive its local proxy capability. Configure authentication for desktop launches, then fully quit and reopen the app. Do not substitute a placeholder. |
| Connection refused at a local address | Check whether the proxy is running and whether the app saved a port from an earlier session. |
| Claude gateway check succeeds but the app fails | The check verifies gateway access. Verify the app's custom-provider support and credential-helper configuration separately. |
| Authentication fails after credentials expire | Run `adp status`; if sign-in is no longer valid, run `adp login`. Confirm the configured app can refresh credentials without copying a token. |
| CLI works but the app fails | Test the app from Finder. The CLI launcher supplies settings and authentication to its own process; desktop startup has a separate environment. |
