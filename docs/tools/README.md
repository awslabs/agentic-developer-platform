# Use ADP tools

This guide covers ADP tools in Claude Code, Codex, crew pods and engine workers.
Tools use your existing ADP identity and the tools your organization permits;
there is no separate AgentCore login.

**Availability:** the CLI and tools gateway are implemented in this repository,
but this guide has not been followed against a deployed tools release. A local
test or an MCP registration is not evidence that a deployed tool is available.
Get the address of a released, tools-enabled ADP gateway from your administrator.
The L2 evaluation must still check the clean-laptop journey against that gateway.

Separately, the [Claude Code and Codex gateway spike](spikes/agentcore-gateway-clients.md)
records operator-supplied dev observations from 2026-10-15, measured shim latency
and source-level timeout findings. Both pinned clients discovered tools, and 100
sequential shim calls were confirmed; only Claude Code completed a client-driven
search. These observations do not verify this guide's clean-laptop journey, the
published installer or Crew pod acceptance. Streaming, edge limits and token
refresh remain unverified.

| Read | For |
|---|---|
| [architecture.md](architecture.md) | end-to-end design: identity, authorization, execution lifecycle/recovery, tools, credentials, deployment and live gates — epic [#7245 `TLS-E1`](https://github.com/aws-e/adp/issues/7245) |
| [deployment.md](deployment.md) | owner-approved dev bootstrap, protected environment, release evidence and rollback; **not a live deployment record** |
| [slack-qualification.md](slack-qualification.md) | private Slack test-channel consent, exact posting, independent readback and owner-approved deletion; **not live evidence** |
| [shared-agentcore-tools.md](shared-agentcore-tools.md) | historical direct-Task REST integration (retired in source; not current worker setup) |
| [agentcore-integration.md](agentcore-integration.md) | AgentCore Browser / Code Interpreter / Web Search integration notes |

## Start on a laptop

You need an ADP account, the authorized gateway address, `curl`, `jq`, `python3`,
and either Claude Code or the Codex CLI already installed. You do **not** need
AWS credentials or the AWS CLI. In a terminal, replace the fictional domain
below with your gateway's base URL (without `/api`):

```sh
ADP_URL=https://gateway.example.com
curl -fsSL "$ADP_URL/api/cli/install.sh" | sh -s -- --gateway-url "$ADP_URL/api"
adp login
adp status
```

The installer puts `adp` and its bundled MCP server in `~/.adp/bin`, adds that
directory to your shell path and remembers the gateway address. If `adp` is not
found, start a new terminal or add `~/.adp/bin` to `PATH`. `adp login` asks you
to approve sign-in in the browser; `adp status` shows the selected local session.
To inspect the installer first instead of piping it to `sh`, run
`curl -fsSL "$ADP_URL/api/cli/install.sh" -o adp-install.sh`, read it, then run
`sh adp-install.sh --gateway-url "$ADP_URL/api"`.

### Claude Code

After signing in, with Claude Code installed:

```sh
adp claude setup
claude mcp list
claude
```

`adp claude setup` preserves other Claude settings and registers the `adp-tools`
MCP server. `claude mcp list` checks registration, **not live tool access**. In
Claude Code, ask: “List the tools available from adp-tools. Do not call any
of them.” Confirm the client actually lists authorized names from the released
gateway. An absent server, empty listing or authentication error is a failed
check; see [Troubleshooting](#troubleshooting).

### Codex

After the same sign-in, with Codex CLI installed:

```sh
adp codex setup
codex mcp list
adp codex
```

`adp codex setup` preserves other model and MCP settings and registers the same
`adp-tools` server. `adp codex` starts the local model-auth proxy. In Codex,
ask: “List the tools available from adp-tools. Do not call any of them.” As
with Claude Code, `codex mcp list` checks registration, not tool availability.

For **tools only**, without changing existing model settings, run
`adp mcp configure --client claude-code` or `adp mcp configure --client codex`.
Both clients launch the bundled `adp mcp` shim. Neither setup requires a second
login, a second MCP package or pasted tokens in client configuration.

## Crew session availability

The sandbox image bundles `adp mcp`, and both Crew launch paths register it with a
loopback adapter. The role-free pod still reaches only its dedicated chat gateway.
The gateway relays tools requests after checking the live Crew session and pod; the
façade's signed authorization callback rechecks those proofs and applies tenant
policy. No laptop login or AWS credential is put in the pod. This is implemented
in source; local checks do not establish a deployed search or user-owned receipt.

## Tool catalogue

Discovery returns only tools allowed for your selected ADP identity and tenant.
This table describes source-level capabilities, **not** a promise that every
target is deployed, enabled or granted to you:

| Tool name or family | Purpose | Availability |
|---|---|---|
| `websearch___search` | Search the web | Core target; gateway and provider charges may apply. |
| `code_interpreter___*` | Start a session, execute code, read results and close | Core target; session and budget limits apply. |
| `browser___*` | Start a browser, act on a page, inspect results and close | Core target; actions may cost money. |
| `adp___operations_list`, `adp___operation_status`, `adp___operation_retry`, `adp___artifact_read`, `adp___execution_close` | Recover a saved operation or close an execution | Local shim helpers; gateway reads require the original owner. |
| `knowledge___*` | Search private knowledge | **Not available** on the shared gateway until private target identity forwarding is verified. Existing engine-worker knowledge access is separate. |
| `credentials___*` | List permitted metadata or use a consented credential through a broker | Optional target, **not live-verified**. There is no raw-read gateway tool. |
| `github___get_user`, `jira___get_issue`, `slack___get_user` | Read user details or a Jira issue through a connected account | Optional OAuth targets; require a released connector, approved tool consent, **Available to agents** enabled on the connection, and the user's `read:user`, `read:jira-work`, or `users:read` scope respectively. |
| `slack___post_message` | Post a message as the connected Slack user | Defined in source but currently denied: the available connection grants `users:read`, not the required `chat:write`. |
| `platform___my_flows`, `platform___my_agent_runs`, `platform___my_work_items`, `platform___my_budget` | Read your own ADP state from Agent Chat: delivery flows, agent runs, GitHub issues/PRs you authored or are assigned to (via the tenant's GitHub App installation), your budget position | First-party, read-only, in-gateway (#8113). Available to the chat assistant once an administrator enables them for the tenant and you consent in Settings → Tools; no admin per-user policy write and no cost ceiling is needed. Not callable through the façade or `adp mcp`. |

See [the tools architecture](architecture.md#6-tools) for the complete design;
the live list in your client determines what you can actually request.

## Administrator tool access (repository implementation)

After the gateway and frontend release, organization administrators can open **Tool access**
under Admin to see the selected tenant's deployed gateway catalogue; platform administrators
can select a tenant. Select **Entire tenant** to enable each tool explicitly. New tenants have
no grants and therefore cannot use tools until an administrator enables them. Since #8387 an
enabled tenant grant is also each member's default consent: the page's **Consent** setting
shows whether the tenant follows the platform posture (`granted`, the default) or requires
`explicit` opt-in per user; switching to explicit restores the previous behaviour for every
member of that tenant at once. Choose a team
to inherit that tenant grant or disable the tool for just that team; a team cannot re-enable
a tool that the tenant has disabled. Set an optional positive per-call USD ceiling for the
tenant or team. When both exist, the tighter ceiling applies. A saved edit changes new tool
listings and calls; it cannot undo an already dispatched provider action. If another
administrator changes the same grant first, the edit is refused and the page reloads the
latest setting. Individual consent, connected-account scopes, personal cost policy and
[rate limits](architecture.md#9-budgets-limits-receipts-lineage) remain additional checks;
per-user limits are not edited on this page.

The admin catalogue displays connector names and credential kinds reported by READY gateway
targets, but these are informational, not separate switches that release credentials or
undeployed operations. Dynamic connectors may not list individual tools until discovery.
The page and API are implemented and locally tested, **not yet confirmed in a deployed
release**. Live L5 evaluation must deny `browser` for a disabled team while an authorized
team retains access; a local mock or test database does not prove that outcome in production.

## Consent, receipts and costs

In ADP Settings → Credentials, switch **Available to agents** on only for
credentials you intend to share with agents. In the Tools tab, every tool your
organization enables for your team is **on by default** (#8387): switch off the ones you
do not want agents to use, or choose **Manage consent explicitly** to tick tools one by
one as before. Organizations that require explicit consent show only that opt-in list.
New and existing credentials default off; if there is no permitted tool to select,
ask an administrator to check policy. The first-party `platform___*` read tools appear in that tab as soon
as your tenant enables them, even before an administrator has written your
per-user tool policy; consenting to one records the same audited consent
receipt as any other tool, and the chat assistant lists only the tools you
consented to (asking you to enable the rest rather than guessing). Enabling a switch cannot create an entitlement. Turning it off blocks
new authorized calls subject to the released gateway's enforcement, but does
not prove an already-started provider session has stopped. Third-party tools also need their own connection consent and approved scopes after release.
In Settings → Connections, the **Third-party accounts for agents** section lists GitHub, Atlassian
(Jira) and Slack user OAuth status, configured scopes, agent access and last-used time separately
from GitHub App installations. Select Connect to visit the provider's consent page; after you return to the same
signed-in browser tab, ADP shows Connected only once the user-owned pointer appears. If consent is
cancelled, expires or the connection cannot be verified, the page reports an error instead of success.
The provider must first be enabled by an operator, and a connected account alone does not bypass
organization tool policy or enable an undeployed connector.

Select Disconnect and confirm to remove ADP's connection pointer and block new authorized calls
through that pointer. **This does not yet remove the provider token from AgentCore Identity or
terminate a call already in progress.** The confirmation warns that provider consent may remain
active; if you need to revoke it at the vendor, use that vendor's authorized account controls.
No released provider consent flow or revocation has been verified by this local implementation.
Never paste stored keys into tool arguments.

The bundled `adp mcp` bridge advertises version 1.0 client-facing schemas, not
provider Lambda schemas. Search takes a `query` without a caller-supplied request
or execution ID. Interpreter and browser starts return an `operation_ref` and a
session handle; subsequent calls accept the start's `operation_ref` as
`execution_ref` alongside that session handle. The bridge resolves the owned
execution under the original login before calling the façade. Do not supply
signed assertions or nonstandard top-level MCP identity fields.

A call has a durable `operation_ref` (its server-owned request ID) and, when
accepted, an `execution_id` and authorization receipt. A `confirmed` result reports an observed outcome;
`unknown` means the outcome is uncertain and may already have incurred a
charge. After an interrupted call, run `adp mcp operations` to find locally
saved references. Ask the client for `adp___operation_status` with the saved
`operation_ref`, then `adp___artifact_read` if a confirmed result is available. If
no execution ID was saved, an **explicit** `adp___operation_retry` reuses the
saved `operation_ref` and original arguments; do not automatically submit a fresh paid call.
A `closing` or `unknown` status does not establish provider termination. These
The bridge still accepts the older local recovery argument name `request_id` for
compatibility, but does not advertise it to new clients. These recovery steps
have local fake-gateway tests, not deployed pinned-client acceptance evidence.
See [execution lifecycle](architecture.md#5-execution-lifecycle-and-recovery-target).

The opt-in `SCN-8064-04` runner selector reserves a separate MCP client and at most one
stateful session for each CLI, Claude Code and Codex cell. It refuses a cell
without confirmed authority, pinned client version and private pricing, and
caps the entire run at $20, each session at 300 seconds, and cleanup readback
at 600 seconds. Running the selector alone does not exercise pinned clients:
the coordinator must bind private per-client fixtures, ordinary-MCP client
adapters and an independent provider observer in the protected workflow after
release. Missing bindings remain `not-run`; synthetic receipts and unconfirmed
provider cleanup cannot establish live acceptance.

Repair scenarios use story-qualified global IDs because both #8064 and #8065
retain their original local `SCN-01`–`SCN-04` and `AC-01`–`AC-04` mappings:

| Global selector | Story | Local scenario / acceptance | Coverage |
| --- | --- | --- | --- |
| `SCN-8064-01` | #8064 | SCN-01 / AC-01 | Client-facing schemas and stateful calls |
| `SCN-8064-02` | #8064 | SCN-02 / AC-02 | Foreign session/operation denial |
| `SCN-8064-03` | #8064 | SCN-03 / AC-03 | Lost-reply recovery |
| `SCN-8064-04` | #8064 | SCN-04 / AC-04 | Pinned CLI, Claude Code and Codex clients |
| `SCN-8065-01` | #8065 | SCN-01 / AC-01 | Entitlement, user policy and consent routes |
| `SCN-8065-02` | #8065 | SCN-02 / AC-02 | Access denials without provider dispatch |
| `SCN-8065-03` | #8065 | SCN-03 / AC-03 | Revision conflicts and target removal |
| `SCN-8065-04` | #8065 | SCN-04 / AC-04 | Bounded grant/consent/revoke/disconnect lifecycle |

Evidence records the global ID, issue number, local scenario ID and acceptance
tag. Unqualified `SCN-01`–`SCN-04` selectors are ambiguous and rejected.
`--scenario-filter repair` selects all eight repair cases; `SCN-8064-*` and
`SCN-8065-*` select individual story families. Existing `SCN-TLS-*`, `full` and
`l5` selectors retain the original TLS suite and do not qualify these repairs.

Final qualification must enumerate all eight global IDs and retain a result for
every selected client cell. It must run `SCN-8065-04` in a separate protected
invocation to preserve its $20 run bound; a combined live `repair` invocation
fails with that instruction before starting a client. The repair suite mixes
local integration checks and opt-in live checks. `SCN-8065-01`–`SCN-8065-03` run
without live mode; they remain blocked if requested as deployed acceptance.
Selecting `repair` or setting
live mode does not supply missing adapters, authorized fixtures or independent
observers, and synthetic results do not establish deployed acceptance. Missing
fixtures remain `not-run` or `blocked` and fail qualification.

ADP activity records approved calls with an authorization receipt and
**estimated** cost, not a final provider bill or proof that work finished. The
published AgentCore Gateway rates in this story are $0.005 per 1,000 tool
invocations and $0.025 per 1,000 searches, **plus** applicable tool-provider
charges. User and tenant rate/budget caps can return HTTP 429 with `Retry-After`
before a provider call; follow that delay instead of sending a new paid request.
Ask your administrator for your actual policy and prices.

After the gateway release, an administrator with `activity:read_all` can request
`GET /api/admin/tool-calls/export?since=2026-10-13&until=2026-10-13&format=csv`
(or `format=parquet`) using their normal gateway sign-in. The dates are inclusive UTC days;
`tenant_id` is optional and checked against the administrator's permitted organization scope.
The download includes `call_id`, `task_id` and `story_id` where verified, the tool name,
user/team/tenant IDs, an authorization receipt, timestamp and `estimated_cost_usd`.
A laptop call without a bound task leaves its task and story empty. A day with no approved calls
returns a schema-only file; unavailable storage returns an error, not a successful empty file.
These are authorized-call estimates, not provider bills or evidence that a tool finished.
This route is implemented in source but requires the protected gateway release and L5 live
evaluation before availability can be claimed.

## Troubleshooting

| What you see | What to check |
|---|---|
| `adp: command not found` | Start a new terminal or add `~/.adp/bin` to `PATH`; check the installer reached your authorized gateway. |
| Expired or missing session, HTTP 401 | Run `adp status`, then `adp login`. Do not print or share `adp token`. |
| `adp-tools` absent | Re-run `adp claude setup` or `adp codex setup`, then check `claude mcp list` or `codex mcp list`. |
| Registered server, but empty or unreachable tools | Check the selected gateway has a released tools façade, your account and tenant are correct, and policy and consent permit tools. Registration alone does not deploy a service. |
| HTTP 403 or `not_consented` | Check organization permission, user consent and any connection scopes; do not bypass the gateway with a provider URL. |
| `scope_not_granted` on a vendor tool | The authenticated user's connected account lacks the tool's required OAuth scope or has no active connection. Slack posting requires `chat:write`, which current connection consent does not request; selecting the tool cannot add it. |
| HTTP 429, `Retry-After` or budget cap | Wait the reported delay; ask an administrator about limits instead of sending a new paid request. |
| Timeout, `unknown` result or uncertain close | Use `adp mcp operations` and the saved reference to read the receipt; do not repeat paid work with a new ID. |

`adp tools status` shows the selected login's token expiry. To stop using ADP
tools, run `adp tools logout`: it requests remote refresh-token revocation and
removes the local token and only ADP's client MCP entries. If revocation cannot
be confirmed, it keeps the credential for a retry. Ordinary `adp logout` only
clears local tokens. Neither command proves an active provider session stopped.

## For crew pods and engine workers

**Wave 3 is not yet live-verified.** The shared tools route for platform agents
must be released and tested before treating the following as working pod or
worker setup. A laptop's `adp login` and MCP entry are **not** workload
credentials. The platform supplies each agent's identity and checks that the
underlying run is still active; a different user, Task or ended lease cannot
borrow its access. Do not copy a laptop token into a pod or grant a pod/worker
direct access to AgentCore.

### Chief-of-Staff pods

After the wave 3 release, a pod launched for your active crew lease uses its
mounted `adp mcp` shim with a session-scoped loopback adapter. The platform configures
the adapter URL, local token and Crew run binding; `adp mcp` detects Crew proxy mode
and does **not** call `adp token` or require an interactive `adp login`. The adapter
uses the existing chat-gateway connection, not a pod AWS role. The façade signs
its authorization callback; the gateway derives the user and tenant from the live
Crew binding. Discovery filters tools by policy, and every call is authorized again.
If your pod's
client shows no `adp-tools` server, the pod image/lease setup needs fixing;
do not enter your personal tokens into it. When available, check the server
with `claude mcp list`, then ask the client to list available ADP tool names
without calling them. Listing a server alone does not verify a live tool call.
An expired or revoked lease must prevent further actions, including after
reconnecting; do not restart paid actions with new IDs to test recovery.

### Dev Box hosts (EC2)

A Kiro Crew session on your ADP Dev Box has the same `adp-tools` server as a pod,
with no `adp login` on the box (#8380). The Crew bundle ships the `adp mcp` shim;
the box's model supervisor registers it against the loopback adapter it already
uses for models, bound to the current host run. Each tool request leaves the box
as a signed `POST /agent/crew/tools` call carrying the instance identity and the
gateway's 60-second model token; the gateway relays it to the façade under a
short-lived host assertion and the authorize plane resolves you from the live
lease. Discovery is filtered by your tools policy and every call is authorised,
consented and receipted under your user, exactly as from a laptop. When the box
stops, is replaced, or its lease ends, tools stop with it. If the dashboard's
client shows no `adp-tools` server, the bundle or release is stale; do not
enter a personal token on the box.

### Engine workers

Engine Task hosts send provider calls to the signed `POST /agent/tools/call`
gateway route. The host supplies its own run credential and workload proof; the
façade asks the gateway to resolve the human and returns an attributed receipt.
The old `ADP_TOOLS_VIA` flag and direct Task-envelope fallback are gone. A gateway
refusal or uncertain outcome must not trigger a direct Lambda call or replay a
paid operation. The façade still accepts REST `POST /tools/{tool}` for compatible
clients, but that path is not the old Task-only Lambda endpoint.

A developer's MCP registration alone does not change a worker's authorization
or install tools in its image. Source tests with a fake façade do not establish
that a release is live: before switching an environment, confirm the L3 workload
gate and drain or safely handle existing sessions. The L5 live check must prove
a deployed Task call works with the user's receipt and direct Lambda invoke is
denied. Engine knowledge access uses the run-bound gateway bridge rather than
worker-supplied Door identity headers.
See [identity and routes](architecture.md#3-identity-one-person-three-clients).

### Gateway Lambda tools (source checkpoint, not live availability)

The façade's provider dispatch reuses the existing tool targets; the interceptor
verifies the gateway assertion before each call and must fail closed on a mismatched
identity. The old Task-only Lambda REST routes are retired in source; the façade
retains REST compatibility routes. Source tests use a synthetic provider response;
they do not prove a released gateway `tools/list` or provider call. The [live evaluation](architecture.md#11-increments-and-live-gates)
checks the deployed routes and user-attributed receipts separately.

## Third-party connections

**Repository checkpoint, not a released connection feature.** In Settings →
Credentials, a saved GitHub PAT displays **Migrate to GitHub connection**. This
starts user-bound consent; the browser keeps the one-time state and Identity
session handle in session storage until the GitHub return redirects to Settings → Connections,
where the signed-in client completes the connection and refreshes its status. The old PAT stays in the
vault until you remove it; this does not copy its value into OAuth. The GitHub
App installation on Settings → Connections is separate from this personal
OAuth connection. Provider apps and the Identity return URL still require
private operator configuration. A callback landing alone never grants access,
and the source tests do not establish a working deployed provider round-trip.

**Connector targets (GitHub, Jira, Slack), also a repository checkpoint.** The gateway now has
user-authenticated start and completion APIs for GitHub, Atlassian (Jira) and
Slack OAuth; an administrator still needs to configure the provider apps and
Identity return URL privately. Start returns an authorization URL and one-time
state. After provider consent, the signed-in browser client must forward the
Identity return session and that state to ADP's authenticated completion API;
the callback landing alone does not connect the account. The Settings →
Connections UI is separate work. Optional vendor OpenAPI targets are implemented in source but have not
been live-verified. A Jira read accepts `cloudId` and `issueIdOrKey` arguments and, when
permitted, returns issue JSON with a receipt. The gateway checks the user-owned connection's
scope on every vendor call before forwarding. Source tests simulate this path; an enabled-provider
Terraform plan and deployed target-level OAuth behavior have not been verified.
Do not paste vendor tokens into a client or agent prompt. The broker resolves a
user token through Identity only after ADP's current ownership, tool consent
and destination checks; connecting does not grant a tool permission that policy
denied. Other providers and their connectors have separate readiness checks.

The first GitHub PAT tool request per tenant starts a persisted grace window,
`BG_GITHUB_PAT_GRACE_DAYS` (default 30). After that window, ADP refuses new
`github/github-pat` bearer delivery through proxy, gated raw-read and operation-
bound paths and audits the denial with a pointer to Settings → Credentials. Settings no
longer offers new PAT registration, but the general vault registration API has
not changed. Already copied PATs are **not** revoked at GitHub: revoke them
there. Setting the grace duration to zero denies delivery on first use.

**Disconnect is incomplete as token revocation.** The API removes ADP's
connection pointer and cancels pending consent, so requests that depended on
that pointer return `403 not_connected` without another selected credential;
an explicitly selected legacy PAT works only during its tenant grace window.
The response states `identity_token_revoked: false`: the token remains in
Identity because the available API lacks a per-user delete operation. New STS
credentials are capped at 900 seconds, but the installed Identity OAuth API
cannot request or report a provider-token lifetime. Do not treat disconnect as
vendor token destruction, assume an issued provider token expires within 15
minutes, or use this PR as evidence of live availability.
For the private GitHub path, `private_assertion_max_seconds: 30` describes the maximum
lifetime of an already signed ADP call assertion, not the provider token. Every new
private call rechecks the current connection, so an assertion cannot authorize a
new call after disconnect; an in-flight call may already have reached the provider.
`provider_access_token_expires_at: null` means the provider-token lifetime is unknown.
`provider_token_revocation: not_attempted` and `revocation_complete: false` also
appear in the disconnect audit. This is deliberate, not a claim that vendor tokens
were already invalid. In the installed AgentCore models, the runtime supports
retrieving a user OAuth token and completing consent but has no user-token deletion;
the control-plane `DeleteOauth2CredentialProvider` deletes the shared provider,
not one user's credential. GitHub's [app-token deletion API](https://docs.github.com/en/rest/apps/oauth-applications#delete-an-app-token)
requires the OAuth app client credentials, which Identity holds in protected
configuration and does not delegate to the gateway. Slack's [auth.revoke](https://docs.slack.dev/reference/methods/auth.revoke/)
can target an access token but cannot demonstrate deletion of the refresh grant
held by Identity. No Atlassian per-user revocation endpoint or authorized
application credentials are configured for this gateway. Until an approved
user-scoped provider deletion path and independent readback exist, disconnect
does not call any vendor revoke endpoint and must not be described as complete
revocation. Account-owner input is needed for protected app access and consent;
the coordinator owns deployed provider evidence.
The L4 evaluation owner must check consent, permitted Jira calls, expiry and
denial on the *deployed* release, after the per-user revocation requirement is
resolved. See the [connection design](architecture.md#8-third-party-connections--agentcore-identity-behind-the-vault).

## Platform deployment smoke

The platform deploy workflow now requires target synchronization, live MCP discovery, and one
authorized `websearch___search` call before publishing its release. Configure the selected protected
dev or prod environment's `ADP_TOOLS_EVAL_GATEWAY_URL` and `TLS_EVAL_REFRESH_TOKEN` for an isolated
test user with search permission, reusing the live-evaluation authentication flow. The façade must
already support live discovery and point to the gateway being deployed; otherwise publication fails.
The deployment role does not gain direct AgentCore access. Matching discovery reads surround the
search call, and their digest plus the target synchronization revision are recorded in `release.json`.
An old façade, broken discovery, failed search or mismatched gateway/list prevents release publication.
This smoke check is not the full L2 evaluation or a D3 receipt; those retain their separate owners.

## Protected live evaluation (source checkpoint)

`tools-live-evaluation.yml` is a dispatch-only test of the **deployed** tools
route, not a laptop smoke test or a deploy command. The engine (or an authorized
manual dispatch on `main`) supplies the full `adp_source_revision` of the deployed
façade, full `gateway_revision`, full `adp_definition_revision` of the running
workflow, and `adp_correlation`. `scenario_filter` is a scenario-ID glob (default
`SCN-TLS-00*`); `full` and `l5` include the exact earlier gate cases and
all 0050–0059 lifecycle cases. A filter with no registered cases fails. The workflow has no
pull-request or scheduled live trigger. `Tools CI` runs only local tag-lint and
runner tests for a PR.

Before dispatch, the operator must verify the approved revisions and configure the
protected `adp-tools-deploy-dev` GitHub environment with the scoped deployment
role, expected account, Kubernetes cluster/namespace, HTTPS ADP gateway base URL
(`ADP_TOOLS_EVAL_GATEWAY_URL`) and `TLS_EVAL_REFRESH_TOKEN` secret for an
isolated, authorized test user. The environment reviewer releases the job; an
agent has no AWS role. Missing configuration fails the job rather than choosing
a fallback gateway or identity. The job imports the test-user session privately,
checks gateway and façade deployed image digests against source-tagged images,
then calls the bundled `adp mcp` shim. It removes runner-owned credentials afterward.
No evaluation infrastructure is created merely to run the tests.

The workflow retains `tools-live-evaluation-<run-id>-<attempt>` evidence in the
runner's private CI evidence S3 store for 14 days. An authorized operator uses the
versioned manifest and checksum in the run's **CI evidence** summary to retrieve
the archive containing `evidence.json` (per-scenario tags, status and deployed
images) and `junit.xml`; this is not a GitHub Actions artifact download.
The `workflow-evaluation/v1`-shaped report is **not** a machine-accepted
orchestration receipt. Failed, skipped or not-run cases do not qualify an
increment; local fixtures cannot prove live provider behavior. The L2/L3/L5
evaluation owners must review the real-client and independent provider-cleanup
evidence before recording acceptance. At this source checkpoint, only
`SCN-TLS-0017` executes a basic MCP tool-list probe. The 0050–0059 lifecycle
recipes are selected but remain not-run until an authorized isolated fault
driver and independent provider observations are available; they do not establish
Claude Code/Codex compatibility or browser/interpreter lifecycle success.
