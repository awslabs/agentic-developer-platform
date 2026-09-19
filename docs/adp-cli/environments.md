# Choose and switch ADP environments

[CLI guide](README.md) · [Command reference](command-reference.md)

**Availability:** this page describes [PR #5449](https://github.com/aws-e/adp/pull/5449),
checked at revision `aeb4692f4` on 19 September 2026. It is not in the checked
`main` build yet. Your installed `adp help` must list `deployment` before you
use these commands. Updating only helps once your gateway serves that build.

## Register your environments once

```bash
adp deployment add development --url https://dev.example.com/api
adp deployment add integration --url https://integration.example.com/api
adp deployment add preprod --url https://preprod.example.com/api
adp deployment list
```

Replace these example URLs with your real gateways. Registration is local and
does not deploy anything, contact the gateway or sign you in. Each distinct
gateway keeps its own login. Sign in once per environment:

```bash
adp --deployment development login
adp --deployment integration login
adp --deployment preprod login
```

Native Cognito administrators can use `admin login` in place of `login`.

## Keep everyday commands short

Choose a saved default:

```bash
adp deployment use development
adp codex setup
adp codex
```

Switch it when needed:

```bash
adp deployment use integration
adp codex
adp status
```

The saved default controls new commands in any terminal without an explicit
selection. It is shared across terminals; it is not limited to newly opened
terminals. Already-running agents and helpers remain pinned to the deployment
they started with.

## Use three environments in three terminals

Use the same Linux/macOS user, home directory and installed ADP CLI. Select a
deployment once in each terminal; subsequent commands stay short.

Terminal 1:

```bash
export ADP_DEPLOYMENT=development
adp codex
```

Terminal 2:

```bash
export ADP_DEPLOYMENT=integration
adp codex
```

Terminal 3:

```bash
export ADP_DEPLOYMENT=preprod
adp claude setup
adp claude
```

To change a terminal's selection, export another name before starting the next
command. To make that terminal follow the saved default again:

```bash
unset ADP_DEPLOYMENT
adp status
```

Changing `adp deployment use` does not override an exported `ADP_DEPLOYMENT`.
Avoid putting one fixed selection in your shell startup file if you want each
terminal to start with the shared default.

## Override one command

```bash
adp --deployment preprod bedrock status
adp --deployment integration admin github status
```

Put `--deployment` before the command. It overrides that terminal's selection
for the one command and its children.

| Selection | Priority and effect |
|---|---|
| `adp --deployment NAME …` | Highest priority for a new command |
| `export ADP_DEPLOYMENT=NAME` | Applies to subsequent commands in that terminal |
| `adp deployment use NAME` | Shared saved default when neither override is set |
| Original single-deployment configuration | Legacy fallback when applicable |

Children inherit the parent's resolved deployment rather than choosing again
mid-command. An unknown explicit name fails; it does not fall back to another
environment. `adp status --json` includes the selection metadata.

## Tools, credentials and cleanup

Run setup after signing in and before first use of each tool in the chosen
environment. The launchers can use different deployments concurrently without
rewriting shared tool settings. Bare `claude` or `codex` uses the deployment
configured by its last setup; changing the shared default does not retarget
those bare commands. Use `adp claude` and `adp codex` for environment selection.

Each deployment has its own session, refresh state and Codex proxy. Do not force
one `ADP_PROXY_PORT` for concurrent environments. On macOS, named deployments
also have separately installed proxy daemons.

```bash
adp --deployment integration refresh
adp --deployment development logout
adp deployment list --json
```

Refresh and logout affect only the selected deployment. To forget one locally:

```bash
adp deployment use development
adp deployment remove integration
```

Removal refuses the saved default or a deployment with a running command,
proxy or installed daemon. Stop those first. It does not remove the remote ADP
environment. Removing the last name for a deployment removes its private local
session and state; the original legacy store is retained.

Two names registered with equivalent gateway URLs are aliases of one deployment
and share a session. They are not independent test environments. The original
single-deployment configuration remains usable as `default` without copying
tokens or requiring another login.

Updating replaces the one shared CLI installation. Rolling back to a build
without named-deployment support leaves the saved records in place, but that
older CLI cannot select them until you update forward again.

## ADP environments, AWS profiles and Superplane workspaces

| Setting | What it selects |
|---|---|
| ADP deployment | The ADP gateway and the session to use |
| AWS `--profile` | Local AWS role credentials for provisioning an account connection |
| `adp superplane workspace use NAME` | A Superplane workspace within the selected ADP deployment |

For example, `adp --deployment integration admin bedrock connect --account
123456789012 --org example-org --profile aws-admin` operates on the integration
ADP gateway and uses local `aws-admin` credentials for AWS provisioning.
