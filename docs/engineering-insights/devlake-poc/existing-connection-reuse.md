# Reuse existing ADP connections

Owner direction confirmed on 2026-11-01: ADP already supplies GitHub and Jira
connectors. Where the POC uses GitHub/Jira, reuse those connections. The later
[provider expansion](provider-expansion.md) selects SonarQube/Jenkins/Argo CD for
the primary connector proof. This existing-connection refinement belongs to the
[canonical architecture](architecture.md) and is proposed for review in existing
[repair #9437](https://github.com/aws-e/adp/issues/9437). Implementation and live
acceptance are still incomplete.

## Verified starting point

These paths were inspected at ADP `cd4dc6d6b83c08c85c63da6927de9e5890c2e8d9`.

| Existing component | Reuse and limitation |
|---|---|
| Gateway `product_onboarding/source_discovery.py` | Uses existing GitHub installations. The generic list omits the current `atlassian-mcp` kind; that omission does not mean ADP lacks Jira. |
| Gateway `internal/connector_source_broker.py` | GitHub collection uses the existing installation client. Jira accepts only a `jira` / `basic_auth` vault entry and calls Jira directly. This is a POC integration gap. |
| Gateway `internal/tool_adapter_authority.py` | Checks personal Atlassian ownership, consent, site, scopes, connection revision and adapter compatibility. Existing tool admission requires an authorized user/run, not an arbitrary background worker. |
| Adapter `modules/tools/adapters/tool_adapters/atlassian.py` | Personal OAuth Jira search and issue reads through reviewed MCP operations. Board discovery is absent from its current personal read allowlist. |
| Gateway `products/jira_context.py` | Product/Project mappings and constrained shared Jira reads. Its browser context response does not request all collection fields. |
| Gateway `auth/atlassian_system_routes.py`; adapter `jira_system.py` and `forge_jira.py` | Existing Product-authorized shared access, Identity retrieval, service-account MCP and Forge REST transports. |

Personal OAuth, shared service-account and Forge connections retain their own
authority. Do not convert a personal connection into shared Product access or
send its MCP OAuth token to Jira REST. Current deployment availability and
provider capabilities require separate observation.

## Required integration

```text
Existing ADP connection and selected Product/Project source
  -> current source authority and explicit collection grant
  -> existing ADP adapter/provider transport
  -> bounded, versioned observation page
  -> optional DevLake-derived Go extraction/normalization
  -> isolated cell, immutable evidence and Insights
```

Profile discovery/enrichment keeps its actor authority and works without
collection. Historical ingestion requires its separate collection grant.
A board URL or successful interactive read does not create that grant.

1. Resolve existing connection IDs and saved Jira mappings. Preserve owner, site,
   connection revision and binding generation. Keep normalized tool identity
   `jira` distinct from the actual connection provider, such as `atlassian-mcp`.
   A provider-string alias alone is not integration.
2. Extend the existing adapter contract with a narrow **collection-read purpose**
   authorized from persisted work, delegation, grant, cell and binding. This is
   proposed implementation. Reuse current personal consent/ownership checks or
   shared Product allowlisting, and recheck before dispatch and page admission.
   Do not impersonate a human to call the existing interactive tool endpoint.
3. Keep access retrieval in the existing Identity/adapter path. Go receives no
   credentials. GitHub retains installation authority; Jira uses the transport
   belonging to its connection method. No duplicate login, basic-auth fallback,
   alternate owner or new gateway vendor HTTP client.
4. Probe reviewed provider capabilities for project/board reads, issue fields,
   classification and continuation. Add only missing bounded operations to the
   existing adapter. Distinguish unsupported capability, missing delegation,
   unavailable deployment and missing implementation. Establish the exact gap
   before requesting user setup. Never invent board membership, classification,
   timestamps or completeness from a search response.
5. Define the versioned page schema in the existing shared adapter contracts.
   Proposed request fields bind operation/attempt, source/binding generation,
   connection revision, entity kind, opaque cursor, limit and deadline. The core
   derives provider arguments; callers supply no arbitrary URL or JQL. Responses
   bind these identities to provider operation, observation time, source-data
   digest, items, continuation and coverage. Conversion to DevLake input records
   its mapper version and original observation digest; converted compatibility
   bytes are not raw provider bytes.
6. Preserve actual provider cursors, including Jira continuation tokens, in the
   existing durable operation/checkpoint journal. Bind them to tenant, owner,
   source, connection revision and query. Do not guess numeric offsets. Retry
   under the same operation; rotation, revocation, changed selection or module
   disable rejects stale pages. Use the smaller provider/adapter/POC bound.

Existing personal tool admission and shared Product context do not yet provide
scheduled collection. The new read-purpose integration is developer-owned work;
it cannot be replaced with a raw user token or deferred to operator configuration.

## Delivery and verification

Keep the current repair owner and release campaign. #9437 owns adapter-to-producer
integration under AC-03 and capability/configuration preflight under AC-04.
Original S03, S10, S11 and S12 owners retain live acceptance. Preserve every
AC/SCN ID and all 41 pilot/44 closure criteria. The subsequent [provider amendment](provider-expansion.md), expanded to seven
providers on Nov 4, adds EC-11 and supersedes the original primary pair. GitHub/Jira
compatibility work must not make a new Jira setup the expansion blocker.

| Planned files / existing areas | Purpose |
|---|---|
| Gateway `product_onboarding/source_discovery.py`, `products/source_routes.py`, `products/jira_context.py` | Reuse connection/mapping identity in the optional path. |
| Gateway `internal/connector_source_broker.py`, `connector_context_routes.py`, `connector_work_authority.py`, `connector_collection_routes.py`, `tool_adapter_authority.py` | Bridge current connection authority and explicit collection work. |
| `modules/tools/adp_tools/adapter_contracts.py`; adapter `atlassian.py`, `jira_system.py` and existing routing/authority consumers | Shared read-purpose/page contract and missing provider operations; existing Identity retrieval. |
| `modules/connectors/devlake/`; `modules/insights/poc/devlake/cmd/collector-worker/` | Consume versioned pages/cursors, demonstrate actual pinned Go reuse, classify before persistence. |
| Existing gateway Product/connector tests, adapter tests and S07 source/preflight integration | Verify real entry-point wiring with controlled observations, followed by the live campaign. |

These file plans do not expand operations repair authority. The coordinator
includes final source/component predecessors and required shared-contract
deployment in the native launch amendment. Preserve passed nodes, receipts,
financial supplements and approved limits. Deploy only changed components in
compatible order within the coordinated campaign; add no per-story deployment.

First developer checkpoint: publish the connection-kind/capability matrix and
page/authority field mapping. Then prove the same connection ID reaches adapter,
pinned Go mapping and criterion consumer, with no credential-create call. Refuse
wrong tenant/Project/site/board, revoked consent, stale revision/cursor and missing
classification. Cover connector-only and separately granted collection paths;
disabling the optional module must preserve ordinary ADP GitHub/Jira tools.

Missing live provider access can block live validation. It does not block this
integration's implementation and entry-point tests with controlled doubles.
