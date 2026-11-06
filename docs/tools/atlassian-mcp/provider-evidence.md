# Provider and client evidence — 2026-10-20

## What is established

Public unauthenticated requests were made from the design workspace. They establish the advertised contract, not authentication or deployed ADP behavior. No registration POST, OAuth exchange, vault write, provider mutation or deployment was attempted. Raw downloaded pages/headers are transient local research, not public acceptance evidence; the sanitized observations and response hashes below are the retained design record. A later fetch can legitimately have a different hash.

| Source / method | Observed result | Meaning and limit |
|---|---|---|
| `GET https://mcp.atlassian.com/v2/mcp?tools=all` | HTTP 401; `WWW-Authenticate` points to `https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp` | Actual v2 resource metadata location, not a guessed root-host contract. No tools were listed without consent. |
| `GET` that protected-resource document | HTTP 200 JSON; resource is `/v2/mcp`, bearer method `header`, one authorization-server issuer | Follow and pin the returned issuer, including its path. Do not substitute legacy Jira 3LO audience/issuer. |
| RFC 8414 authorization metadata for that issuer | HTTP 200 JSON; `client_id_metadata_document_supported=true`; `registration_endpoint`, `authorization_endpoint`, `token_endpoint`, `revocation_endpoint`; S256; `authorization_code` and `refresh_token` advertised | CIMD and DCR are advertised alternatives, not proven working registrations. Other advertised machine grants are outside this epic. |
| Same issuer metadata | Token endpoint auth methods: `none`, `client_secret_post`, `client_secret_basic`, `private_key_jwt` | Proof must pin the selected client authentication mode. No inference that ADP's built-in vendor provider supports these semantics. |
| Protected-resource scopes | `read:me`, `read:account`, `offline_access`, Jira read/write/search agent-interface groups, Confluence read/search groups, plus many excluded groups | Availability is not granted consent. Reject unrelated/admin/delete scopes in ADP's requested/approved set. |
| [Official getting-started guide](https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/) and [supported tools](https://support.atlassian.com/atlassian-ai-gateway/docs/supported-tools/) | `?tools=all` supplies a paginated flat catalogue for gateways; named required reads/writes documented | Use the flat named tools, not discover/execute wrappers. Authentication is needed to capture exact JSON Schemas and actual allowed catalogue. |
| [Official OAuth guide](https://support.atlassian.com/atlassian-ai-gateway/docs/configure-oauth-2-1/) | Interactive user consent; scopes/app access; cloud ID/site enforcement and possible domain/redirect allowlists | Normal setup must automate client configuration. The guide's copied-bearer example is **not** the ADP user journey. |

### Response hashes

SHA256 of response body bytes as retrieved (not request headers or credentials):

| Artifact | SHA256 |
|---|---|
| v2 protected resource JSON | `d4687eded5e691a672019ff34ea3feeaef3aab895362b8061d05544d2a870d44` |
| v2 issuer authorization metadata JSON | `aab7a57bc585f29197f8f4f929ab94abfa60179b2dc58d17bdf6f2adef09d839` |
| Atlassian getting-started HTML | `0986995a629c21797913720e5ffbb905ce840374f7c887eeffdf0748319e923b` |
| Atlassian OAuth HTML | `dadfa4021cb741c774d735ea0e46e92ceb8b22ccafc1ac1e5c7dce7674ac9297` |
| Atlassian supported-tools HTML | `9c0f07c976713c7483649c29ea78322822f306e69f30a42ae22666168a9c5555` |
| Codex MCP HTML | `fd40421670e4d5ad00f29000700a8ab4ebb75696f75c8784297eb3462cd72203` |
| Codex config-reference HTML | `c17c9a2ed0cca4b20c322870ab760f35bb6b9d66c629001745bf914f01817f6a` |
| Claude MCP Markdown | `3a93df9b555274bc92ef495b65e127ff07c128bd044606a70880e9bf948a2d60` |

## Identity reuse decision — 2026-10-21

The user requires reuse of AgentCore Identity. Source at the audited baseline shows `connections_routes.py` starts user federation with `get_resource_oauth2_token` and completes it with `complete_resource_token_auth`; `internal/credential_routes.py` retrieves tokens through Identity. `shared/identity/oauth_setup.py` already configures `AtlassianOauth2` for legacy Jira and `CustomOauth2` for Slack. These are reuse evidence, not proof that either configuration supports Atlassian MCP v2.

Design v3 requires Identity to own OAuth consent orchestration, provider callback/exchange, user-token storage and refresh. ADP retains owner/site metadata and application return/completion. #8446 must prove the selected built-in/custom Identity configuration, including issuer/resource/PKCE, registration mode, expiry/concurrent retrieval, session recovery and supported remote revocation. CIMD support advertised by Atlassian does not establish Identity compatibility. No authenticated Identity experiment was run for this amendment; unsupported requirements must be recorded as blockers. V2's separate ADP OAuth engine, user-token secrets and refresh jobs are superseded.

## Separate adapter placement — 2026-10-21

The user selected a separate shared tool-adapter service. This changes ADP packaging, IAM/transport and release boundaries; it does not prove new Atlassian or Identity capabilities. The [service contract](adapter-service.md) retains Identity-managed OAuth and adds a required proof that a separately scoped adapter role can retrieve the resolved user's token after core admission. #8446 can prove this with isolated roles/resources without deploying successor code. #8448/#8449 must separately prove real core-to-adapter serialization/admission, and #8453/#8454 must prove private route destination, independent release and live isolation. Direct Identity access working in the core process is insufficient evidence for this new boundary.

## Client observations

The [Claude MCP page](https://code.claude.com/docs/en/mcp#configure-tool-search) describes native tool search but disables it by default for non-first-party `ANTHROPIC_BASE_URL` endpoints. An override is not proof the gateway forwards `tool_reference` correctly. Its feature availability also depends on model and managed settings. Therefore this design does not carry forward an unconditional schema-deferral promise from vendor documentation.

The fetched [Codex MCP page](https://developers.openai.com/codex/mcp/) documents stdio MCP server configuration, timeouts and named-tool allow/deny filters. The [config reference](https://developers.openai.com/codex/config-reference/) says retired `tool_search` and `tool_search_always_defer_mcp_tools` switches are rejected. These pages do not establish native schema deferral in ADP's custom-provider path. No installed-client model-facing schema measurements were taken. A future qualified build may support it; the initial contract permits a measured eager fallback, not an invented switch or generic execute tool.

PR #8392's description reports previous Claude work. It was inspected as a source dependency, not rerun as acceptance evidence. At inspection its head was `21a0a2fdb06035eb8d5728e3014a1b2137eb8b72`, status open/draft, reviews empty. Main already contains setup-time Claude/Codex MCP configuration at `modules/gateway/cli/adp:1157`; automatic session-scoped launch behavior and collision protection are separate requirements.

## Unresolved checkpoints and ownership

| Gap | Owner / exact action | Affected phase |
|---|---|---|
| Isolated Identity resources, ADP return host and consenting licensed fixture not supplied | Epic lead supplies private probe configuration; account owner consents; policy admin participates only if required. #8446 builds its own return/completion listener, Identity client and metadata ledger; no successor route or release is needed | #8446 authenticated run and dependent auth implementation acceptance. Probe construction and design/review can proceed independently. |
| Identity built-in/custom provider compatibility, automated registration, resource/PKCE and managed vault/refresh/revoke unproven | #8446 executes the Identity-only section 2 probe and pins sanitized SDK/provider/cleanup evidence; no direct OAuth fallback. #8447–#8449 separately prove the real production routes/workers/mediator | #8446 AC-01, #8447, #8448 readiness. A prototype pass proves feasibility, not downstream integration. |
| Exact granted tool schemas/site semantics and subset enforcement unobserved | #8446 retrieves all authorized pages, checks required names/scopes and produces sanitized schemas/digests; independently checks subject/sites | #8446 AC-02, final adapter implementation contract in #8449/#8450. |
| Provider write idempotency/uncertain-outcome oracle not established | #8446/#8450 qualify each verb or retain conservative unknown/no-replay behavior | Mutation/recovery qualification; never blocks the no-blind-replay rule. |
| Claude/Codex minimum builds and schema loading through ADP unqualified | #8452 pins actual binaries and performs separate protocol/byte measurements and AWS-free calls | Client release qualification/EC-05, not a reason to block backend design. |
| Two users/sites, policy-denial and cleanup permissions not supplied | Epic lead provisions private fixture manifest; account owners grant consent; #8454 acceptance owner runs independent provider assertions | Live acceptance; no claim that the sites do not exist. |
| Independent review has not completed | Existing PR review owner reviews exact design commit; architect resolves findings and updates story links/status | Approved design handoff. No author self-approval. |

The requested checkpoint is **partially resolved: public provider contract verified; Identity-managed MCP compatibility unproven and its live checkpoint needs target/consent**. No credential search, substitute account or production action is an acceptable way to fill this gap. Keep #8446 and dependent readiness explicit until the actual proof and review are recorded.

## Coordinated rollout decision — revision 5

On 2026-10-21 the user reported no active consumers of existing connectors and requested migration without a scheduled maintenance window. The design now groups Atlassian MCP, GitHub, Slack and legacy Jira into one release after implementation, provider proof and readiness/parity checks. This is user-supplied rollout context, not independently observed usage or authenticated provider evidence. AgentCore Identity remains required; none of the unresolved checkpoints above is satisfied by this decision. No deployment or runtime change was performed.
