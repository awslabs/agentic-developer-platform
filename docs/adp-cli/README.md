# ADP CLI guide

Use `adp` to sign in to an ADP deployment, run Codex or Claude Code, connect AWS
accounts and GitHub repositories, and administer model access and GitHub setup.

Organization names such as `example-org`, repository names and account IDs in
this guide are placeholders. Replace them with your own values.

## Start here

Get your ADP gateway URL from your administrator or your deployment's sign-in
page. Replace `adp.example.com` below with that host; include `/api` as shown.

```bash
curl -fsSL https://adp.example.com/api/cli/install.sh -o /tmp/adp-install.sh
sh /tmp/adp-install.sh --gateway-url https://adp.example.com/api
# Open a new terminal if the installer updated your PATH.
adp login
adp status
adp codex setup
adp codex
```

For Claude Code, use `adp claude setup` followed by `adp claude`. Install the
underlying tool separately; the ADP installer installs the ADP CLI. One ADP
login serves both tools. See [installation and sign-in](getting-started.md) for
prerequisites, headless login and administrator login.

## Find a task

| I want to… | Read |
|---|---|
| Install, sign in, launch tools, update or uninstall | [Getting started](getting-started.md) |
| Choose a default environment or use three terminals | [Environments](environments.md) |
| Look up every command and its options | [Command reference](command-reference.md) |
| Connect an AWS account, reuse a role or arrange an administrator handoff | [AWS accounts and Bedrock](aws-and-bedrock.md) |
| Set a Bedrock route for an organization, team or user | [AWS accounts and Bedrock](aws-and-bedrock.md#configure-bedrock-routing) |
| Configure the platform's GitHub App or connect my repository | [GitHub](github.md) |
| Use Superplane workspaces, deployments and provider credentials | [Superplane](superplane.md) |
| Submit, monitor or abort work through Task APIs | [Task API commands](tasks.md) |
| Automate commands or diagnose errors | [Scripting and troubleshooting](scripting-and-troubleshooting.md) |

## Command structure

```text
adp login / status / refresh / logout
adp codex [tool arguments]
adp claude [tool arguments]
adp aws <command>
adp github <command>
adp bedrock status
adp admin login / setup
adp admin bedrock <command>
adp admin github <command>
adp superplane <area> <command>
adp task submit / status / monitor / abort
```

`adp codex` and `adp claude` remain short, everyday commands. Administration
lives under `adp admin`. An AWS `--profile` selects local AWS credentials for
provisioning; it does not select an ADP deployment or sign you in to ADP.

## Availability

This guide was checked on 19 September 2026 against `main` revision
`3d01efb45` and the command parsers in [the CLI source](../../modules/gateway/cli).
Your installed CLI and gateway both need to support the operation you request.
Use `adp help` and the relevant area's `--help` to inspect your installation.

**Environment selection is currently in [PR #5449](https://github.com/aws-e/adp/pull/5449),
not the checked `main` revision.** The [environment guide](environments.md)
documents that implementation, including the saved default and terminal
selection. Do not assume `adp update` provides it until your deployment serves
that CLI build. `adp version` alone does not distinguish these builds.

Superplane commands are present in the CLI but require the corresponding domain
API on your deployment; see [its availability notes](superplane.md#availability).
There is currently no `adp issue` or `adp agent` command for submitting work to a
cloud agent. GitHub connection commands configure access; they do not start an
agent run.

Task submission is available through `adp task` in the CLI build described in the
[Task guide](tasks.md), using a separately registered Task service identity.
