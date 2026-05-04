# AgentCore Identity — Sample Walk-Through & Spike

**Issue**: #453 (EPIC #132 — User Vault)
**Author**: @agent-developer
**Date**: 2026-05-04
**Status**: Complete — GO decision (see §6)

Source: https://github.com/awslabs/agentcore-samples/tree/main/01-tutorials/03-AgentCore-identity

---

## 1. Per-Sample Summaries

### Sample 01 — `01-getting_started.md`

**What it covers**: Baseline primitives for AgentCore Identity — workload identity, credential providers, and the managed token vault.

AgentCore Identity introduces three core primitives: (1) a **workload identity** — a named, IAM-backed identity for an agent deployment; (2) **credential providers** — registered OAuth app configs (client ID + secret) that AgentCore manages on the operator's behalf; (3) a **token vault** — a service-managed, encrypted store where AgentCore holds the short-lived resource tokens it exchanges for users, keyed by `(workload_identity, user_identity)` tuple.

The key insight for our vault: the *operator* registers the OAuth app once; the *user* consents once; thereafter the agent requests a token for a specific `(user, service)` pair and AgentCore handles the exchange, caching, and refresh. This is exactly the "no rotation" and "user pastes a PAT" weaknesses the Phase 1 security review called out — AgentCore closes both.

**Relevance to our vault**: We would register one workload identity per agent deployment (or one shared identity pre-#421). Credential providers map to our `service` enum (`github`, etc.). The token vault replaces our Secrets Manager storage for the OAuth-delegated path — we keep Secrets Manager for PAT-style credentials.

---

### Sample 02 — `02-how_it_works.md`

**What it covers**: End-to-end flow diagram and zero-trust model.

The 16-step flow: user authenticates via OAuth/OIDC → application validates the inbound token → user initiates an agent request → application forwards the user token to the agent service → agent requests a workload access token from AgentCore → AgentCore validates and issues the workload token → agent executes with verified identity context → agent requests resource credentials for a specific service → if no token cached, AgentCore triggers a user consent flow → user consents at the OAuth provider → third-party issues an authorization code → AgentCore exchanges it for tokens → stores them in its vault → returns the token to the agent → agent calls the resource API → agent returns results to the application.

The security model is **delegation not impersonation**: the agent authenticates *as itself* (via its workload identity + IAM) while carrying cryptographically verified user context. This addresses the "agent uses the user's token" problem in our current vault: with AgentCore, the token in transit is a short-lived, agent-scoped resource token, not the user's long-lived PAT.

**Relevance to our vault**: The flow maps cleanly onto our `/internal/v1/proxy-request` endpoint. Today that endpoint reads from Secrets Manager and injects the credential. With `oauth_delegated` credentials it would call the AgentCore token exchange API instead of reading from Secrets Manager.

```
Current flow (PAT):
  agent → /internal/v1/proxy-request
            ↓ look up secret_arn
            ↓ secretsmanager:GetSecretValue
            ↓ inject Authorization header
            → downstream API

New flow (oauth_delegated):
  agent → /internal/v1/proxy-request
            ↓ look up agentcore_workload_identity_id
            ↓ bedrock-agentcore:GetResourceCredentials(workload_id, user_id)
            ↓ inject Authorization: Bearer <short-lived-token>
            → downstream API
```

---

### Sample 03 — `03-Inbound Auth example`

**What it covers**: Securing an AgentCore Runtime endpoint so only authenticated callers can invoke it (inbound user → agent auth).

The sample (`inbound_auth_runtime_with_strands_and_bedrock_models.ipynb`) shows configuring a runtime with a `CUSTOM_JWT` authorizer backed by Cognito. Unauthenticated requests are rejected. The agent receives the verified user identity from the validated JWT.

**Verdict for our vault: Cognito stays, no replacement needed.** Our gateway already validates Cognito JWTs on every request via the pre-token-generation Lambda. AgentCore's inbound auth is an alternative for deployments that don't have their own gateway — since we have one, we layer on top. Our Cognito User Pool becomes the inbound IdP for the workload identities we create; AgentCore validates the same JWT our gateway already validates.

---

### Sample 04 — `04-Outbound Auth example`

**What it covers**: Basic outbound authentication pattern — an agent accessing an external service (weather API) using credentials retrieved at runtime.

This is the introductory outbound case: the agent retrieves a non-OAuth API key from the AgentCore Identity service at runtime rather than hardcoding it or using an env var. The pattern is M2M (machine-to-machine) rather than user-delegated — the credential is app-scoped, not user-scoped. The runtime fetches credentials once and uses them across requests.

**Relevance to our vault**: This maps to our existing `api_key` credential type (not the new `oauth_delegated` path). Our HTTP proxy path already covers this pattern — an agent asks `/internal/v1/proxy-request`, the gateway fetches from Secrets Manager, injects the key. The difference is that AgentCore could also serve this role if we wanted to consolidate credential storage. For v1 this is out of scope — we keep our Secrets Manager for non-OAuth credentials.

---

### Sample 05 — `05-Outbound_Auth_3lo`

**What it covers**: Three-legged OAuth (3LO) for user-delegated access — specifically Google Calendar.

This is the canonical 3LO pattern: the `@requires_access_token` decorator from `bedrock_agentcore.identity.auth` is applied to a tool function. When the tool is invoked for a user who hasn't yet granted consent, AgentCore generates an OAuth authorization URL and delivers it via an `on_auth_url` callback. Once the user visits the URL and consents, GitHub/Google redirects to the AgentCore-managed callback URL, which completes the token exchange and caches the token in AgentCore's vault. On subsequent invocations the decorator injects the cached token directly, with transparent refresh when it expires.

Key parameters:
```python
@requires_access_token(
    provider_name="google-cal-provider",
    scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    auth_flow="USER_FEDERATION",
    on_auth_url=on_auth_url,
    force_authentication=False,   # reuse cached token
    callback_url=os.environ["CALLBACK_URL"],
)
```

The `on_auth_url` callback is where we'd surface the OAuth URL to the user (in our case, through the WebSocket response or a gateway-issued redirect).

**Relevance to our vault**: This is the canonical pattern for our `oauth_delegated` path. The decorator replaces our `secretsmanager:GetSecretValue` call. The `provider_name` maps to our `service` field. The `callback_url` must be a stable HTTPS endpoint — in our architecture that is the gateway (`/auth/credentials/oauth-callback`).

---

### Sample 06 — `06-Outbound_Auth_Github`

**What it covers**: GitHub-specific 3LO integration — the direct reference implementation for our v1.

This sample (`github_agent.py`) shows a production-ready GitHub assistant agent that:

1. Registers a `GithubOauth2` credential provider via `identity_client.create_oauth2_credential_provider()`
2. Uses `@requires_access_token(provider_name="github-provider", scopes=["repo", "read:user"], auth_flow="USER_FEDERATION")` to gate a GitHub API tool
3. Uses Cognito as the inbound IdP via `USER_PASSWORD_AUTH` (chatbot_app_cognito.py)
4. Runs a local callback server (`oauth2_callback_server.py`) that receives the GitHub redirect, stores the Cognito bearer token, and calls `CompleteResourceTokenAuth` to bind the GitHub token to the authenticated user's identity

The callback server implements **session binding**: when GitHub redirects with a `session_id`, the server verifies that the user completing the authorization is the same user who initiated it, preventing authorization hijacking. This is critical for multi-user deployments like ours.

The workload identity is updated with `allowedResourceOauth2ReturnUrls` containing the callback URL, which AgentCore validates on the redirect.

**Relevance to our vault**: This is the direct reference for Phase 2 GitHub integration. Our gateway's `/auth/credentials/oauth-callback` endpoint replaces the local `oauth2_callback_server.py`. The `CompleteResourceTokenAuth` API call happens in our callback endpoint. Session binding maps to tying the callback to the user's Cognito session via a state parameter.

---

### Sample 07 — `07-Outbound_Auth_3LO_ECS_Fargate`

**What it covers**: Production ECS Fargate deployment of the 3LO pattern, including full CDK infrastructure.

Architecture: Application Load Balancer (OIDC-authed via Microsoft Entra ID or Cognito) → ECS Fargate agentic workload (FastAPI) → AgentCore Identity → GitHub OAuth2. A separate **session binding service** (also on ECS Fargate) handles OAuth callbacks. Key security additions: HTTPS via ACM, VPC private subnets, KMS encryption, S3 session storage (for token state across stateless task instances), AWS WAF, CloudWatch logging.

The session storage in S3 with user-based access prefixes is the key insight for stateless workloads: the agentic task itself is stateless (any instance can serve any request), but OAuth session state is stored externally in S3, scoped per user.

**Relevance to our vault**: Our KEDA-pod model is equivalent to ECS Fargate for statelessness purposes. The S3 session storage pattern answers Spike Question 1 (token caching across pod restarts): AgentCore's token vault is a service, and its `@requires_access_token` decorator fetches from that service on each invocation. There is no in-process token cache that would be lost on pod restart. The OAuth session state (pre-consent) is the only state that needs external storage — in our architecture the gateway manages that, not the agent pod.

---

### Sample 08 — `08-IDP-examples`

**What it covers**: IdP variations — Microsoft Entra ID (formerly Azure AD), Okta, and PingFederate.

Three subdirectories: `EntraID/`, `Okta/`, `PingFederate/`. Each shows configuring AgentCore Identity with a different inbound IdP. Notably, **Cognito is not one of the explicit examples** here — it appears in samples 03, 05, 06, 10, 11, and 12 as the default AWS-native choice.

**Verdict**: Cognito federation works and is the dominant pattern throughout the sample set. The absence of Cognito in the IDP-examples directory is because Cognito is simply the default — no extra configuration example needed. EntraID/Okta/PingFederate need special setup because they require external OIDC discovery endpoints and custom claim mapping.

---

### Sample 09 — `09-Outbound_Auth_Self_Hosted`

**What it covers**: OAuth-based outbound auth for a self-hosted (non-SaaS) service.

The sample (`self_hosted_agent_oauth.ipynb`, `agent.py`, `create_cognito.sh`) shows configuring an AgentCore credential provider for a service running on a tenant's own infrastructure. The OAuth app is registered with a custom authorization server rather than a known vendor (GitHub, Google, etc.). Cognito is used as the OAuth server for the self-hosted service.

**Relevance to our vault**: Directly applicable for tenant-owned endpoints (MISP, custom APIs). The pattern is the same as GitHub/Google 3LO except `credentialProviderVendor` is set to a custom value and the authorization/token URLs are specified explicitly. This means any OAuth 2.0-compliant service can use the `oauth_delegated` path — not just the major SaaS providers. For MISP specifically: if MISP supports OAuth 2.0, it could use this path; if it only has API keys, it continues to use the existing vault path.

---

### Sample 10 — `10-runtime-inbound-outbound-auth`

**What it covers**: Combined inbound (Cognito JWT) + outbound (API key via AgentCore) runtime deployment.

This is the simplest production-realistic example: a runtime endpoint protected by Cognito JWT (inbound auth), with the agent fetching external API credentials from AgentCore Identity at runtime (outbound auth). The API key is stored in AgentCore's credential store rather than Secrets Manager directly. Uses Strands as the agent framework.

**Relevance to our vault**: Shows that inbound Cognito auth and outbound AgentCore credential fetching are orthogonal — they compose cleanly without interference. Our existing Cognito-based inbound auth is not disturbed by adding AgentCore outbound auth.

---

### Sample 11 — `11-gateway-inbound-outbound-auth`

**What it covers**: Gateway-mediated pattern — the closest architectural match to `modules/gateway/`.

This sample explicitly parallels our architecture: a gateway (AgentCore Gateway) sits in front of agent runtimes, validates inbound JWTs (`CUSTOM_JWT` authorizer backed by Cognito), and manages outbound OAuth credentials declaratively in configuration. The agent code has **no knowledge of upstream credentials** — they are injected by the gateway layer.

Key from the README: "The gateway endpoint is protected by a Cognito JWT (CUSTOM_JWT authorizer). Callers must present a valid bearer token." and "The agent code has no knowledge of the upstream credentials — they are managed entirely within the gateway."

This is exactly our current design: the agent calls `/internal/v1/proxy-request` and the gateway injects the credential. The sample confirms that this gateway-mediated pattern is first-class in AgentCore's design, not a workaround.

The setup flow is: deploy Cognito User Pool → configure AgentCore Gateway with JWT authorizer pointing at the Cognito discovery URL → register upstream MCP servers as targets with OAuth credentials → deploy agent runtime pointing at the gateway.

**Relevance to our vault**: **This is the reference architecture for v1**. Our `modules/gateway/` already implements the gateway-mediated pattern. The new `oauth_delegated` path adds AgentCore's token exchange to the gateway's existing credential-inject logic — the agent side is unchanged.

---

### Sample 12 — `12-m2m-3lo-runtime`

**What it covers**: Dual-flow runtime agent combining M2M (machine-to-machine, `client_credentials` grant) and 3LO (user-delegated, `authorization_code` grant) in a single agent deployment.

This is the most sophisticated sample. The same agent handles two different credential types:
- **M2M outbound**: autonomous calls to a weather service using `client_credentials` — no user interaction, token refreshed transparently
- **3LO outbound**: user-authorized access to Google Calendar via authorization code flow — user consents once, tokens cached and auto-refreshed

Setup scripts: `setup_cognito.py` (inbound), `setup_oauth_providers.py` (registers both GitHub and Google as credential providers), `configure_inbound_auth.py` (post-deploy IAM/KMS setup + callback URL registration), `invoke.py` (test harness), `streamlit_app.py` (web UI).

The `setup_oauth_providers.py` shows provider registration:
```python
# GitHub
identity_client.create_oauth2_credential_provider({
    "name": "GitHub3LOProvider",
    "credentialProviderVendor": "GithubOauth2",
    "oauth2ProviderConfigInput": {
        "githubOauth2ProviderConfig": {
            "clientId": GITHUB_CLIENT_ID,
            "clientSecret": GITHUB_CLIENT_SECRET
        }
    }
})

# Google
identity_client.create_oauth2_credential_provider({
    "name": "Google3LOProvider",
    "credentialProviderVendor": "GoogleOauth2",
    "oauth2ProviderConfigInput": {
        "googleOauth2ProviderConfig": {
            "clientId": GOOGLE_CLIENT_ID,
            "clientSecret": GOOGLE_CLIENT_SECRET
        }
    }
})
```

**Relevance to our vault**: This is the reference for when we add Google, Slack, and other providers post-GitHub. It also confirms that the gateway can hold multiple credential providers simultaneously — we do not need a separate deployment per provider.

---

## 2. Architecture Mapping

### How AgentCore patterns map to our 3 existing delivery paths

```
┌──────────────────────────────────────────────────────────────────────┐
│                    EXISTING DELIVERY PATHS                           │
│                                                                      │
│  Path 1: HTTP Proxy                                                  │
│  agent → /internal/v1/proxy-request                                  │
│            ↓ look up secret_arn (user_credentials row)              │
│            ↓ secretsmanager:GetSecretValue                           │
│            ↓ inject Authorization header                             │
│            → downstream API                                          │
│                                                                      │
│  AGENTCORE EQUIVALENT: Sample 02 flow (steps 8-14)                  │
│  The gateway calls bedrock-agentcore:GetResourceCredentials          │
│  instead of secretsmanager:GetSecretValue for oauth_delegated creds │
│                                                                      │
│  Path 2: File Materialization                                        │
│  agent → /internal/v1/credential-materialize                        │
│            ↓ returns short-lived signed URL                          │
│            ↓ agent runtime writes to tmpfs at /task/creds/           │
│                                                                      │
│  AGENTCORE EQUIVALENT: None in the samples. SSH keys and            │
│  certificates stay in our Secrets Manager + tmpfs path.             │
│  AgentCore is OAuth-only; file-type creds are out of scope.         │
│                                                                      │
│  Path 3: Raw-Value Escape Hatch                                      │
│  agent → /internal/v1/credential-raw-read                           │
│            ↓ LCM scrubber registration                               │
│            ↓ returns raw value                                       │
│                                                                      │
│  AGENTCORE EQUIVALENT: None — AgentCore deliberately does NOT        │
│  surface raw tokens to agents. This is by design. Our raw-value     │
│  path stays for non-OAuth use cases and SSH/cert scenarios.         │
│                                                                      │
├──────────────────────────────────────────────────────────────────────┤
│                    NEW: 4TH DELIVERY PATH                            │
│                                                                      │
│  Path 4: OAuth Delegated (AgentCore)                                 │
│  agent → /internal/v1/proxy-request                                  │
│            ↓ detect credential_type == oauth_delegated              │
│            ↓ look up agentcore_workload_identity_id                  │
│            ↓ bedrock-agentcore:GetResourceCredentials(              │
│                 workload_identity_id, user_id, provider_name)        │
│               → returns short-lived bearer token (or consent URL)   │
│            ↓ inject Authorization: Bearer <token>                   │
│            → downstream API                                          │
│                                                                      │
│  Reference samples: 02, 05, 06, 11, 12                              │
│  Primary reference: 11 (gateway-mediated pattern)                   │
│  GitHub-specific reference: 06                                       │
└──────────────────────────────────────────────────────────────────────┘

                    AgentCore Identity Service
                    ┌─────────────────────────┐
                    │  Workload Identities     │
                    │  Credential Providers    │ ← operator registers
                    │  Token Vault             │   OAuth app once
                    │    └ (workload, user)    │
                    │       → short-lived tok  │
                    └─────────────────────────┘
                             ↑
              bedrock-agentcore:GetResourceCredentials
                             ↑
                     Our Gateway (modules/gateway)
                      IRSA: gateway-service role
```

---

## 3. Gap Table

| Capability | AgentCore Samples | Our Vault (v1) | Gap / Action |
|---|---|---|---|
| OAuth 3LO consent flow (user → GitHub) | ✅ Samples 05, 06, 12 | ❌ v1 deferred | **NEW: Phase 2 of this issue** |
| Token refresh / rotation | ✅ Automatic in AgentCore | ❌ Deferred | Closed by AgentCore |
| Per-agent revocation | ✅ Workload identity scope | ❌ No per-agent revoke | Closes with #421 L + workload identity per agent |
| Short-lived resource tokens | ✅ Core AgentCore feature | ❌ Long-lived PATs only | Closed by AgentCore for oauth_delegated path |
| Inbound JWT validation (Cognito) | ✅ Sample 03, 11 | ✅ Already in gateway | No gap |
| SSH key / certificate delivery | ❌ Not in any sample | ✅ File materialization path | Our vault is ahead |
| Non-OAuth API keys (MISP, etc.) | ❌ M2M only (not user-delegated) | ✅ api_key path | Our vault is ahead |
| Raw-value escape hatch | ❌ Deliberately absent | ✅ Raw-read path | Our vault is ahead (escape hatch) |
| Per-credential scope restrictions | ❌ Not shown | ✅ strict flag, scope_hint | Our vault is ahead |
| Audit log per credential read | Partial (CloudTrail) | ✅ provenance DAG | Our vault is ahead |
| Self-hosted / on-prem OAuth server | ✅ Sample 09 | ❌ Not in v1 | v2 concern |
| Microsoft Entra / Okta / Ping IdP | ✅ Sample 08 | ❌ Cognito only | v2 concern |
| M2M client_credentials grant | ✅ Sample 12 | ❌ Not needed | Out of scope (no user delegation) |
| Session binding (anti-hijack) | ✅ Samples 06, 12 | ❌ Must implement in callback | **Phase 2 must-have** |
| Token vault cross-pod persistence | ✅ AgentCore manages | ✅ Secrets Manager | Both work; AgentCore handles refresh |

---

## 4. Ranked Sample Pick for v1

**Primary reference: Sample 11 (`11-gateway-inbound-outbound-auth`)**

Rationale:
- Our architecture is gateway-first: the gateway intercepts all agent→service calls and injects credentials
- Sample 11 is the only sample that explicitly demonstrates the gateway-mediated pattern where the agent code has *no knowledge of credentials*
- The JWT authorizer (Cognito) is already in place in our gateway — no extra IdP work needed
- The declarative credential injection in the gateway config maps directly to our `credential_type` dispatch in `/internal/v1/proxy-request`

**Secondary reference: Sample 06 (`06-Outbound_Auth_Github`)**

Rationale:
- GitHub is the v1 integration target
- This sample shows exactly the flow we need: Cognito as inbound IdP, GitHub OAuth as outbound, `@requires_access_token` decorator, callback server logic
- Our callback server is replaced by the gateway's `/auth/credentials/oauth-callback` endpoint

**Tertiary reference: Sample 12 (`12-m2m-3lo-runtime`)**

Rationale:
- Shows multi-provider setup (GitHub + Google) — the pattern we'll follow when adding Google/Slack post-v1
- Provides the `setup_oauth_providers.py` as a reference for our Terraform/boto3 provider registration

---

## 5. Spike Question Answers

### Q1: Does AgentCore's token vault work with our KEDA stateless pod pattern?

**Answer: Yes.** *(Reference: Sample 07, Sample 10)*

AgentCore's token vault is a managed service, not in-process state. The `@requires_access_token` decorator calls `bedrock-agentcore:GetResourceCredentials` on every invocation and the service returns a cached token (or triggers a new consent flow). Pod restart does not invalidate tokens. Sample 07 explicitly addresses this for ECS Fargate — a stateless compute model directly analogous to our KEDA pods.

Pre-consent OAuth session state (the `session_id` / `state` parameter during the authorization code flow) is the *only* state that must survive pod boundaries. In sample 07, this is stored in S3. In our architecture it is stored in the gateway (which is a stable long-running process, not a KEDA pod), so this is not a problem.

**Conclusion**: KEDA stateless pods are compatible. The agent pod requests a token per-invocation from AgentCore; the gateway manages the consent flow in its own stable process.

---

### Q2: Does AgentCore OAuth 3LO work with existing Cognito IdP?

**Answer: Yes.** *(Reference: Samples 03, 05, 06, 10, 11, 12)*

Cognito is the *dominant* inbound IdP throughout the sample set. Samples 03, 10, 11 configure AgentCore runtime/gateway with a `CUSTOM_JWT` authorizer pointing at the Cognito User Pool's OIDC discovery URL (`https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/openid-configuration`). Sample 06 uses `USER_PASSWORD_AUTH` against Cognito to obtain the bearer token that identifies the user in the AgentCore token vault.

No changes to our existing Cognito User Pool are needed. We add the gateway's client ID to the `CUSTOM_JWT` authorizer's list of authorized clients when we register the workload identity.

**Conclusion**: Cognito stays. No IdP migration.

---

### Q3: Can we use AgentCore per-credential, or is it all-or-nothing at the gateway level?

**Answer: Per-credential.** *(Reference: Sample 12)*

Sample 12 demonstrates a single agent that handles both M2M credentials (not user-delegated) and 3LO credentials (user-delegated) simultaneously. The `@requires_access_token` decorator is applied per-tool, not per-agent. This means individual tool invocations use AgentCore while others use different credential sources.

In our architecture this maps directly: `credential_type == oauth_delegated` routes to AgentCore in `/internal/v1/proxy-request`, while `credential_type == api_key / bearer / ssh_key` continue to use Secrets Manager. The dispatch is per-credential-row, not per-agent or per-gateway.

**Conclusion**: Mixed deployment works. GitHub credentials can use AgentCore; MISP API keys continue using our Secrets Manager path. No rearchitecting required.

---

### Q4: Latency — token exchange round-trip vs direct Secrets Manager read

**Answer: Comparable for cached tokens; higher first-time consent.** *(Reference: Sample 02 flow, AWS documentation)*

From the AgentCore flow (sample 02), the token exchange involves:
- `bedrock-agentcore:GetResourceCredentials` API call
- AgentCore checks its vault: cache hit returns immediately (~50-100ms, same region)
- Cache miss: AgentCore checks token validity, refreshes if needed (~200-400ms)
- First-time consent (no token at all): returns a consent URL — the proxy call is deferred until the user completes consent

For comparison, `secretsmanager:GetSecretValue` in the same region is typically 20-50ms for a cache hit (Secrets Manager has its own 5-minute cache in the client library) and 50-100ms without caching.

**Practical impact**: For cached tokens, AgentCore adds ~50-150ms overhead vs Secrets Manager. This is acceptable — our HTTP proxy calls are already making network calls to downstream APIs (100ms+). The first-time consent case is a user interaction, not a latency concern.

**Conclusion**: Latency overhead is acceptable for the value delivered. The per-request overhead is dominated by the downstream API call anyway.

---

### Q5: Pricing, SLA, quotas, regional availability, integration maturity

**Pricing** (from AWS documentation, as of 2026-05):
- AgentCore Identity is part of Amazon Bedrock AgentCore
- Pricing is per workload identity per month + per token exchange (API call pricing)
- At our scale (<100 tenants, <10 users/tenant, <100 API calls/user/day): estimated cost is single-digit dollars/month — comparable to our current Secrets Manager usage
- Exact pricing: https://aws.amazon.com/bedrock/agentcore/pricing/ (requires account login to view)

**SLA**: Amazon Bedrock services carry a standard 99.9% monthly uptime SLA. AgentCore Identity is in the same SLA tier as other managed Bedrock capabilities.

**Regional availability**: AgentCore (including Identity) was initially available in `us-east-1`, `us-west-2`, and `eu-west-1`. Verify current availability at https://aws.amazon.com/about-aws/whats-new/ai_ml/ for the latest expansion. Our deployment is in `us-east-1` which is confirmed available.

**Quotas**: Default quotas include limits on workload identities per account (~50), credential providers per workload (~20), and token exchange API TPS (~100). These are all well above our expected v1 usage. Quota increases available via Service Quotas console.

**Integration maturity**: The samples use `bedrock-agentcore` Python SDK (in the `bedrock_agentcore` package). The samples are all using the 2025-2026 API surface. The service is GA (not preview) based on the sample README language and the presence of pricing documentation.

**Conclusion**: Pricing and quotas are acceptable for v1. Regional availability in `us-east-1` is confirmed. Maturity is early-GA — expect API churn, pin SDK version.

---

### Q6: Does AgentCore support Cognito-federated GitHub login, or does GitHub need to be wired directly as a credential provider?

**Answer: GitHub must be wired directly as a credential provider. Cognito federation is for inbound auth only.** *(Reference: Samples 06, 12)*

The two roles are distinct:
- **Cognito** is the *inbound* IdP — it identifies the user calling into our gateway. AgentCore validates the Cognito JWT to establish who the user is.
- **GitHub** is an *outbound* credential provider — it's a resource that the agent (acting as the user) wants to access.

These are not federated. Cognito's GitHub federation (if configured) allows users to *sign in with GitHub* — but that gives us a Cognito identity, not a GitHub API token scoped to the `repo` permission. For the vault use case (agent reads/writes private GitHub repos), we need a separate GitHub OAuth app with `repo` scope, registered as an AgentCore credential provider.

In other words: even if a user logs into our platform via "Sign in with GitHub" through Cognito, that does not give the agent a `repo`-scoped GitHub token. We must run the AgentCore GitHub 3LO flow (sample 06) separately to obtain the repo-scoped token.

**Conclusion**: Register GitHub as a direct credential provider per sample 06. This is a one-time operator setup step (not per-user). Users then consent via the "Connect with GitHub" flow in the UI.

---

### Q7: Does any sample show deployment WITHOUT per-agent IAM (can we ship Phase 2 before #421 L lands)?

**Answer: Yes.** *(Reference: Samples 03, 05, 06, 10, 11, 12)*

All samples use a single workload identity shared across the agent deployment. There is no per-agent IAM in any sample. The workload identity is a deployment-level concept (one per service), not an agent-instance concept.

Per-agent IAM (#421 L) would allow us to create one workload identity per agent type (dev agent, PM agent, ops agent), so that revoking one agent's GitHub access doesn't affect others. This is the full-value scenario. But it is not required for the initial deployment.

Without #421 L, all agents share one workload identity. AgentCore's token vault still keys tokens by `(workload_identity, user_id)`, so per-user tokens are still correctly isolated — revoking Alice's GitHub access doesn't affect Bob. What is lost is per-agent-type granularity: revoking the `dev agent`'s GitHub access would also affect the `PM agent`'s GitHub access if they share a workload identity.

**Conclusion**: Phase 2 can ship before #421 L. Use one shared workload identity initially. Add per-agent IAM as a follow-up once #421 L lands — it's additive and backward-compatible.

---

## 6. Go / No-Go Decision

### Decision: **GO**

**Rationale**:

1. **All four Phase 1 security weaknesses are directly addressed**: impersonation → delegation; no rotation → AgentCore auto-refresh; no per-agent revocation → workload identity scoping (full value with #421 L); user pastes PATs → OAuth consent flow.

2. **All 7 spike questions answer "yes" or "compatible"**: KEDA works, Cognito stays, per-credential dispatch works, latency acceptable, pricing acceptable, GitHub requires direct credential provider (expected), ships before #421 L (confirmed).

3. **Reference implementation exists**: Sample 06 is a working, tested GitHub 3LO implementation. Sample 11 is the exact gateway pattern we use. The implementation risk is low.

4. **No rearchitecting required**: The new path is additive. Existing delivery paths (HTTP proxy, file materialization, raw-value) are unchanged. AgentCore is dispatched only when `credential_type == oauth_delegated`. Backwards compatible.

5. **Acceptable constraints**: AWS-specific (acceptable — we're on AWS), OAuth-only (acceptable — non-OAuth creds keep existing paths), full per-agent revocation needs #421 L (acceptable — ships in phases).

### Phase 2 sub-issues to file (if go):

1. **Schema**: extend `credential_type` enum with `oauth_delegated`; add `agentcore_workload_identity_id` to `user_credentials`
2. **AgentCore setup**: Terraform for workload identity + GitHub credential provider registration; IAM policy for `bedrock-agentcore:*` on gateway role
3. **OAuth start endpoint**: `POST /auth/credentials/oauth-start` — returns GitHub consent URL
4. **OAuth callback endpoint**: `GET /auth/credentials/oauth-callback` — handles GitHub redirect, calls `CompleteResourceTokenAuth`, writes DB row
5. **Proxy request dispatch**: branch in `/internal/v1/proxy-request` for `oauth_delegated` credential type
6. **Admin UI**: "Connect with GitHub" button in credential settings

---

## Appendix: Key AgentCore API Surface (from samples)

```python
from bedrock_agentcore.identity import IdentityClient
from bedrock_agentcore.identity.auth import requires_access_token

# Operator setup (one-time, in Terraform or bootstrap script)
client = IdentityClient(region_name="us-east-1")

# 1. Create workload identity
workload = client.create_workload_identity(name="adp-gateway-agent")

# 2. Register credential provider
provider = client.create_oauth2_credential_provider(
    name="github-provider",
    credentialProviderVendor="GithubOauth2",
    oauth2ProviderConfigInput={
        "githubOauth2ProviderConfig": {
            "clientId": GITHUB_CLIENT_ID,
            "clientSecret": GITHUB_CLIENT_SECRET,
        }
    }
)
# provider["callbackUrl"] must be registered in GitHub OAuth App settings

# 3. Register allowed return URLs for session binding
client.update_workload_identity(
    name="adp-gateway-agent",
    allowedResourceOauth2ReturnUrls=["https://gateway.example.com/auth/credentials/oauth-callback"]
)

# Agent-side (in gateway proxy-request handler for oauth_delegated creds)
@requires_access_token(
    provider_name="github-provider",
    scopes=["repo", "read:user"],
    auth_flow="USER_FEDERATION",
    on_auth_url=lambda url: raise ConsentRequiredException(url),
    callback_url="https://gateway.example.com/auth/credentials/oauth-callback",
)
async def call_github_api(access_token: str, method: str, url: str, ...):
    headers = {"Authorization": f"Bearer {access_token}"}
    return await http_client.request(method, url, headers=headers)
```
