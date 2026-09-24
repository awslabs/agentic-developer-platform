# ADP CLI command reference

[CLI guide](README.md) · [Availability and source revision](README.md#availability)

Replace uppercase argument names and example account IDs with your own values.
Square brackets mean optional arguments, not literal shell characters. Use
`adp help` for the top-level help and, for example, `adp aws connect --help` for
a Python command's options. Codex and Claude arguments belong to those tools.

## Authentication, tools and CLI management

| Command | Options / arguments | What it does |
|---|---|---|
| `adp login` | `--gateway-url URL`, `--no-browser` | Browser approval; uses the stored gateway when omitted |
| `adp status` | No options on the checked `main`; `--json` in PR #5449 | Shows local session, gateway and token expiry |
| `adp refresh` | None | Refreshes the selected session now |
| `adp logout` | None | Clears the local session |
| `adp import` | Required `--gateway-url URL`; optional `--client-id ID`, `--user-pool-id ID`, `--region REGION`, `--refresh-token TOKEN` | Imports a browser refresh token; prefer its prompt/stdin over the token flag |
| `adp token` | None | Prints a valid access token for a tool's auth helper; keep stdout private |
| `adp codex setup` | None | Writes ADP's Codex configuration |
| `adp claude setup` | None | Writes ADP's Claude Code configuration |
| `adp codex` | Any Codex arguments | Starts the auth proxy if needed, then launches Codex |
| `adp claude` | Any Claude Code arguments | Checks the ADP session, then launches Claude Code |
| `adp serve` | `--port PORT`, `--foreground` | Runs the local auth proxy in the foreground; legacy default port is 9191 |
| `adp daemon install` | None; macOS only | Installs an always-running launchd proxy |
| `adp daemon uninstall` | None; macOS only | Removes that daemon |
| `adp update` | `--to VERSION`, `--rollback` | Updates from the configured gateway or restores local previous copies |
| `adp version` | Aliases: `adp --version`, `adp -v` | Shows the CLI version |
| `adp help` | Aliases: `adp --help`, `adp -h` | Shows top-level commands |

`--to` accepts an exact `MAJOR.MINOR.PATCH` value (also `--to=VERSION`). It
checks the version served by the gateway; it does not fetch arbitrary past
releases. `--rollback --to VERSION` checks the saved local rollback version.

`setup` is reserved immediately after a tool name. `adp codex -- setup` and
`adp claude -- setup` forward that word to the underlying tool instead.
There is no `adp serve --stop`; use Ctrl-C for a foreground proxy or uninstall
the daemon that owns it.

## Environment selection — PR #5449

These commands require the [named-deployment build](environments.md).

| Command | Options / arguments | What it does |
|---|---|---|
| `adp deployment add NAME` | Required `--url URL`; optional `--json` | Registers a gateway locally |
| `adp deployment list` | `--json` | Lists registrations, session state and selection |
| `adp deployment use NAME` | `--json` | Changes the shared default for subsequent commands |
| `adp deployment remove NAME` | `--json` | Removes a local registration, subject to default/busy checks |
| `adp --deployment NAME COMMAND …` | Selection goes before `COMMAND` | Overrides the deployment for one command and its children |

`export ADP_DEPLOYMENT=NAME` selects a deployment for subsequent commands in
the current terminal. `unset ADP_DEPLOYMENT` restores use of the shared default.
`--deployment` takes priority over the environment variable, which takes
priority over the shared default. `deployment`, `help` and `version` operate on
the CLI itself; do not put `--deployment NAME` before those commands.

## Personal AWS connections

See [AWS workflows](aws-and-bedrock.md#connect-your-aws-account).

| Command | Required inputs | Optional inputs |
|---|---|---|
| `adp aws connect` | `--account ACCOUNT` for a new/imported connection, or `--resume DIRECTORY` | `--name NAME`, `--region REGION`, `--profile PROFILE`, `--role-arn ARN`, one ExternalId input, `--download DIRECTORY`, `--yes`, `--dry-run`, `--json` |
| `adp aws list` | None | `--json` |
| `adp aws verify CONNECTION` | Connection name or ID | `--json` |
| `adp aws disconnect CONNECTION` | Connection name or ID | `--yes`, `--dry-run`, `--json` |

For an existing role, the ExternalId inputs are mutually exclusive:
`--external-id-file FILE`, `--external-id-stdin`, or `--no-external-id` when the
role really has no such condition. With none supplied, an interactive command
prompts. The file/stdin format is `{"external_id":"VALUE"}`.

The default region is `us-east-1`; the default new connection name is
`personal-ACCOUNT`. `--profile` selects local AWS credentials. `--role-arn`
imports an existing role, `--download` prepares an administrator handoff, and
`--resume` verifies a saved handoff. Do not combine these workflows or add
account/profile/setup overrides to `--resume`. `--yes` and `--json` can be used
when resuming. Disconnect removes the ADP connection, leaving its AWS role and
CloudFormation stack in place.

## Administrator login and first-time setup

| Command | Options | What it does |
|---|---|---|
| `adp admin login` | `--credentials-file FILE` **or** `--credentials-stdin`; `--json` | Signs in with a native Cognito administrator account; prompts when interactive |
| `adp admin setup` | `--org ORG`, `--dry-run`, `--yes`, `--json` | Checks and resumes Bedrock and GitHub configuration |

Credential JSON contains `username` and `password`, with challenge fields when
needed. See [protected inputs](scripting-and-troubleshooting.md#protected-inputs).
`admin setup` is the supported guided setup command; there is no `adp pa firsttime`.

## Bedrock destinations and routing

See [routing and administrator handoff](aws-and-bedrock.md#configure-bedrock-routing).

| Command | Inputs | What it does |
|---|---|---|
| `adp bedrock status` | `--json` | Shows your effective account and winning routing level |
| `adp admin bedrock connect` | See options below | Creates/reuses a destination, verifies it, then assigns a routing rule |
| `adp admin bedrock list` | `--org ORG`, `--json` | Lists registered destinations |
| `adp admin bedrock verify DESTINATION` | Destination ID; `--json` | Re-verifies a destination without changing a rule |
| `adp admin bedrock status` | `--user USER`, `--json` | Shows your route, or another user's when authorized |

Connect options:

| Option | Meaning |
|---|---|
| `--account ACCOUNT` | Register a destination in this 12-digit AWS account |
| `--org ORG` | ADP organization ID or exact name; required for new account registration |
| `--team TEAM` | Assign a team rule within that organization |
| `--user USER` | Assign a user rule; accepts ID, email or GitHub username |
| `--destination ID` | Reuse an existing destination instead of provisioning |
| `--profile PROFILE` | Local AWS role session used for direct provisioning |
| `--download DIRECTORY` | Register pending setup and save the role handoff files |
| `--resume DIRECTORY` | Verify and assign the account and scope saved in a handoff |
| `--name NAME` | Role nickname; generated from the organization if omitted |
| `--region REGION` | AWS region; default `us-east-1` |
| `--yes`, `--dry-run`, `--json` | Confirm explicit changes, preview changes, or return JSON |

Omitting `--team` and `--user` selects the organization rule. Those two options
are mutually exclusive. Do not combine `--resume` with account, scope, profile
or other setup overrides. For `--destination`, specify the desired scope; do
not also request `--download`, `--profile` or `--name`.

The top-level `adp bedrock` compatibility group also accepts `connect`, `list`,
`verify` and `status` with the same parser. Use `adp admin bedrock` for
administrative operations; using the compatibility group does not bypass server
authorization. No CLI command currently deletes a destination or removes a
routing rule.

## User GitHub connections

See [connect a repository](github.md#connect-your-repository).

| Command | Required inputs | Optional inputs |
|---|---|---|
| `adp github connect` | `--repo OWNER/REPOSITORY` | `--org ORG`, `--no-browser`, `--yes`, `--dry-run`, `--json` |
| `adp github status` | None | `--repo OWNER/REPOSITORY`, `--org ORG`, `--json` |

`--org` narrows the results to an ADP organization you belong to. Repository
selection and approvals happen on GitHub. Rerun `connect` after approval to
resume. There is no `adp github disconnect` command in this build.

## Administrator GitHub configuration

See [new and existing GitHub Apps](github.md#configure-the-deployments-github-app).

| Command | Options | What it does |
|---|---|---|
| `adp admin github setup` | Options below | Creates an App, imports an existing App or resumes setup |
| `adp admin github status` | `--json` | Reports sign-in, installations and agent integration configuration |
| `adp admin github revalidate` | `--json` | Reads the App's live configuration to identify drift |

Setup accepts `--new` or `--existing`, `--github-org OWNER`,
`--owner org|user`, `--app-name NAME`, `--visibility private|public` (default
`private`), `--org ADP_ORG`, `--yes`, `--dry-run` and `--json`.
Existing-App credentials come from `--credentials-file FILE` or
`--credentials-stdin`; do not combine those sources. The default owner type is
`org`. ADP organization context and GitHub App ownership are separate inputs.

## Superplane

These commands exist in the CLI; [server availability and examples](superplane.md)
explain the domain API dependency. All operational leaf commands accept
`--json`. For workspace-scoped commands, `--workspace` overrides
`adp superplane workspace use NAME` and accepts either the workspace name or its
id; a name that matches more than one workspace is reported with the candidate
ids rather than resolved to one of them.

Workspace and deployment creates persist a non-secret operation receipt before
delivery. An identical retry reuses that operation ID, including after successful
completion, so lost output cannot cause a second resource. Failed, deleting and
deleted operations keep their receipts and refuse identical creates; inspect the
original resource before choosing a different name for an intentional new create.
An older domain without the replay contract is refused before mutation.

| Command | Required inputs | Optional inputs / defaults |
|---|---|---|
| `adp superplane workspace create` | `--name NAME` | `--isolation dedicated|namespace|research` (default `dedicated`), `--account ACCOUNT` (required for `research`), `--budget-daily USD`, `--budget-gpus COUNT`, `--dry-run`, `--yes` |
| `adp superplane workspace list` | None | None |
| `adp superplane workspace use NAME` | Workspace name | Saves the selection locally |
| `adp superplane workspace describe` | Selected workspace or `--workspace WORKSPACE` | None |
| `adp superplane workspace kubeconfig` | Selected workspace or `--workspace WORKSPACE` | Returns Kubernetes access information and the credential's expiry |
| `adp superplane node` | Selected workspace or `--workspace WORKSPACE` | Lists nodes; the command is `node`, without a `list` subcommand |
| `adp superplane quota show` | Selected workspace or `--workspace WORKSPACE` | None |
| `adp superplane quota set` | Selected workspace or `--workspace WORKSPACE`; at least one quota option | `--max-gpus COUNT`, `--max-cost-per-day USD`, `--max-nodes COUNT`, `--allowed-clouds aws,lambda`, `--dry-run`, `--yes` |
| `adp superplane cost` | None | `--org` for organization-wide cost, or `--workspace WORKSPACE`; `--start-date`, `--end-date` (ISO 8601). `--org` and `--workspace` are mutually exclusive |
| `adp superplane events` | None | `--resource-type TYPE`, `--user USER_ID`, `--action ACTION`, `--event-type TYPE`, `--start-time`, `--end-time` (ISO 8601), `--limit COUNT` (1-500, default 50), `--offset COUNT`. There is no workspace filter |
| `adp superplane deploy create` | `--model MODEL`, `--name NAME`; selected workspace or `--workspace WORKSPACE` | `--precision fp8|fp16|bf16|awq|int8` (default `fp16`), `--serving-framework vllm|sglang`, `--replicas COUNT`, `--gpu-per-replica COUNT`, `--tensor-parallel-size COUNT`, `--max-model-len TOKENS`, `--dry-run`, `--yes`. Omitted options take the server's default |
| `adp superplane deploy list` | Selected workspace or `--workspace WORKSPACE` | Uses the workspace namespace |
| `adp superplane deploy delete` | `--id DEPLOYMENT_UUID`; selected workspace or `--workspace WORKSPACE` | `--dry-run`, `--yes`; requests deployment deletion |
| `adp superplane account onboard` | `--name NAME`, `--provider aws`, `--account-id ACCOUNT`, `--credential-id ADP_CONNECTION_ID` | `--dry-run`, `--yes`; registers a verified caller-owned AWS connection through the server-side adapter |
| `adp superplane account list` | None | None |
| `adp superplane account delete ACCOUNT` | The registration's record id, or the cloud account ID or name it was registered under | `--dry-run`, `--yes`; requests deregistration |
| `adp superplane aws-onboard register` | `--account-id ACCOUNT`, `--credential-id ADP_CONNECTION_ID` | `--name NAME`, `--dry-run`, `--yes`; retries converge on the existing matching registration |
| `adp superplane provider add` | `--name NAME`, `--provider PROVIDER` (unless recovering) | `--type api_key|oauth_token|bearer|basic_auth|config_file` (default `api_key`), `--stdin`, `--recover ADP_CREDENTIAL_ID`, `--dry-run`, `--yes`; recovery reuses the recorded id and requests `--stdin` only when the vault confirms the first write is absent |
| `adp superplane provider list` | None | None |
| `adp superplane provider delete CREDENTIAL` | The registration's record id, or the ADP credential id it references | `--dry-run`, `--yes`; removes the provider registration and the vault credential |
| `adp superplane org` | None | Prints a redirect to ADP organization settings; performs no administration |
| `adp superplane user` | None | Prints a redirect to ADP user settings; performs no administration |

Superplane mutations support `--dry-run` and require interactive confirmation
or `--yes`. A provider secret is read from a hidden prompt or stdin, never from a
secret-valued command argument.

## Installer options

These belong to `install.sh`, not the `adp` executable.

| Option | Meaning |
|---|---|
| `--gateway-url URL` | Remember this gateway; alternatively set `ADP_GATEWAY_URL` for installation |
| `--prefix DIRECTORY` | Install under this directory; default `~/.adp/bin` |
| `--no-path-edit` | Print the PATH instruction without changing shell startup files |
| `--version-pin VERSION` | Require the exact served CLI version; alternatively `ADP_VERSION_PIN` |
| `--uninstall` | Remove the CLI files at the selected prefix; keep session data |
| `--help`, `-h` | Show installer help |
| `--version`, `-v` | Show installer version |

## Output and exit codes

`--json`, `--yes` and `--dry-run` are command-specific, not global flags. Put
them after a command that lists them above. The older auth/tool commands do
not implement the Python command groups' shared JSON/exit-code contract.
See [scripting](scripting-and-troubleshooting.md) for examples and exit codes.

`bg-cognito-auth.sh` is the underlying compatibility helper for auth commands;
`bg-auth.sh` is the deprecated SigV4 helper. Routine use should go through `adp`.
Maintainer details remain in [the source README](../../modules/gateway/cli/README.md).
