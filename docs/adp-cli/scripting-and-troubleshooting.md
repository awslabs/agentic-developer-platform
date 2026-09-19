# Scripting and troubleshooting

[CLI guide](README.md) · [Command reference](command-reference.md)

## JSON, previews and confirmation

The `aws`, `bedrock`, `admin`, `github` and Superplane leaf commands support
`--json` where listed in the reference. Standard results contain `status`,
`command`, `detail`, `next_action`, and `error` on failure. Diagnostics go to
stderr so stdout can be parsed.

```bash
adp aws list --json
adp admin github status --json
adp bedrock status --json
```

Use `--dry-run` on a command that supports it to preview the intended change.
Use `--yes` to approve a command with explicit inputs in a script; it does not
supply credentials or complete external AWS/GitHub approvals. Superplane does
not offer these two options. Flags are not global: `adp --json aws list` is not
the supported syntax.

The deployment registry's JSON output in PR #5449 has its own documented
shape: `deployment list` returns fields such as `deployments`, `default` and
`effective` directly. Older auth commands and tool launchers do not use the
Python command groups' JSON contract. `adp token` prints the token itself.

## Exit codes

For the Python command groups:

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | Successful command or completed read | Inspect the result for the requested state |
| 1 | Invalid command or arguments | Check that command's help |
| 2 | Authentication/configuration required | Configure the gateway and sign in |
| 3 | Insufficient authorization | Use the appropriate authorized account |
| 4 | Pending or unavailable prerequisite | Follow `next_action`, then resume |
| 5 | Operation failed | Inspect the structured error and retry after fixing the cause |
| 130 | Interrupted | Check remote state before retrying a mutation |

Some read commands deliberately complete with exit 0 while reporting pending
configuration in their JSON. Inspect `status` and the relevant `detail` fields
as well as the exit code. Auth-core commands retain their own exit conventions;
tool launchers propagate the underlying tool's exit code. Do not interpret a
Codex exit code using this table.

A script that needs to handle a pending handoff can preserve the exit status
without `set -e` aborting before it reads the response:

```bash
umask 077
result_file=$(mktemp)
adp_exit=0
adp aws connect --account 123456789012 --download ./aws-handoff --yes --json \
  > "$result_file" || adp_exit=$?
case "$adp_exit" in
  0) jq '.status, .detail' "$result_file" ;;
  4) jq '.status, .next_action' "$result_file" ;;
  *) jq '.error' "$result_file" >&2 ;;
esac
rm -f "$result_file"
```

## Protected inputs

Native administrator login accepts a private `0600` JSON file owned by you:

```json
{
  "username": "admin@example.com",
  "password": "REPLACE_WITH_THE_COGNITO_PASSWORD"
}
```

```bash
adp admin login --credentials-file /private/admin-login.json --json
# Or consume the same object from protected stdin:
adp admin login --credentials-stdin --json < /private/admin-login.json
```

Create credential files privately; do not commit them. Prefer secret-manager
output piped directly to stdin in automation, and disable shell tracing around
secret handling. Challenge inputs can include `new_password`, `sms_mfa_code`,
`software_token_mfa_code`, and any required attributes named by the challenge.
Interactive login prompts instead; enrollment that cannot finish in the CLI
reports a browser action.

AWS ExternalIds, GitHub App credentials and Superplane provider credentials
also have file/stdin input paths. Use the format documented for that command;
these formats are not interchangeable. `--yes` never removes the requirement
to provide an actual credential.

## Common problems

| Symptom | Check and next action |
|---|---|
| `adp: command not found` | Open a new terminal or add the installer prefix to `PATH`; default `~/.adp/bin` |
| A command group is missing | Check `adp help`; update from a gateway serving a compatible CLI build |
| `deployment` is unknown | Named deployments are in PR #5449; the ordinary `main` build checked for this guide does not contain them |
| A new default does not affect this terminal | Run `printenv ADP_DEPLOYMENT`; `unset ADP_DEPLOYMENT` to use the shared default |
| An agent still uses the old environment | Running sessions remain pinned; launch a new command after selecting another deployment |
| Bare `claude` or `codex` targets a different environment | Bare tools follow their setup; use the `adp` launcher with your selection |
| Not signed in, or refresh expired | Run `adp login` or `adp admin login` against the intended gateway |
| Browser cannot open on EC2 | Use `adp login --no-browser` and approve the printed URL on your computer |
| AWS account mismatch | Check `aws sts get-caller-identity --profile PROFILE`; select an authorized role session for the intended account |
| You cannot create an IAM role | Use the relevant `connect --download DIRECTORY` flow and have an AWS administrator apply it |
| A setup command exits 4 | Read `next_action`; apply/approve the requested external step, then resume |
| A destination fails verification | Have the AWS owner check its role, trust policy and Bedrock permissions; rerun `admin bedrock verify` |
| Bedrock status shows an unexpected account | Inspect the winning user/team/org rule; a personal `aws connect` does not set model routing |
| GitHub App is missing | A platform administrator must run `adp admin github setup` or use Settings → Connections |
| GitHub repository is not granted | Adjust repository access on the existing GitHub installation; rerun `github connect` |
| The proxy cannot bind its port | Check for another listener; stop the owning proxy/daemon before reconfiguring. Avoid a fixed `ADP_PROXY_PORT` across named environments |
| A Superplane command returns unavailable/404 | Confirm the compatible domain API is deployed; having the command in CLI help is insufficient |

## Local files and logs

| Location | Purpose |
|---|---|
| `~/.adp/bin/` | Default CLI installation and helpers |
| `~/.bedrock-gateway/` | Original single-deployment config, tokens and proxy state |
| `~/.adp/state/` | Workflow and handoff state on the single-deployment CLI |
| `~/.adp/logs/proxy.log` | Single-deployment proxy diagnostics |
| `~/.claude/settings.json` | Claude Code settings merged by setup |
| `~/.codex/config.toml` | Codex settings merged by setup |
| `~/.adp/deployments/` | Named-deployment sessions, state, runtime and logs in PR #5449 |

Treat token files and downloaded handoff parameters as private. Share sanitized
errors, selected deployment names, versions and request IDs when reporting a
problem; do not paste `adp token` output or credential files.
