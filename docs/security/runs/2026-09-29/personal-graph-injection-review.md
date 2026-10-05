# Personal-context graph query remediation

Related issue: [#6667](https://github.com/aws-e/adp/issues/6667). Reviewed base: `f3ef80585`.

All four personal-context graph operations now send fixed openCypher query text and separately encoded parameters. The only query identifier selection is a closed, source-defined map of the five existing relationship types. Entry IDs, identity values, vertex values, and edge property maps never become query text. This uses the same Neptune property graph; no new database or feature activation is introduced.

Neptune does not support ordinary bindings on Gremlin text requests. Its [parameterized openCypher HTTP API](https://docs.aws.amazon.com/neptune/latest/userguide/opencypher-parameterized-queries.html) supports a form-encoded `query` plus JSON `parameters`. The [Neptune compatibility documentation](https://docs.aws.amazon.com/neptune/latest/userguide/feature-opencypher-compliance.html) lists the query operations used here. SigV4 signs the exact form body, and existing CA verification is preserved.

Identity and authorization changes:

- Tenant IDs must be 1–128 ASCII letters/digits, underscores or hyphens, beginning with a letter/digit. UUIDs, ULIDs and ordinary organization slugs remain accepted. Invalid values are rejected rather than stripped or coerced, including at direct `CallerIdentity` construction.
- Both the starting and returned vertices in neighbor traversal must belong to the caller's tenant and be caller-owned or shared. An owner match alone no longer crosses tenants.
- Vertex upsert matches owner and tenant as well as entry ID, preventing overwrite of another scope's vertex. Edge creation requires a caller-owned source and a readable same-tenant target. Deletion requires owner and tenant. Edge/deletion calls without identity fail closed.
- Synthesis passes identity explicitly and groups entries by tenant as well as owner/persona, preventing a user's memberships in different organizations from mixing learning groups.

The repository-memory reader from the other half of #6667 already uses `execFileSync` argument arrays and NUL-delimited filenames. Its existing nine regression tests passed; no additional memory implementation change was needed.

Validation:

- 173 personal-context tests passed, including two tests executing real Cypher queries in a disposable local Neo4j 5 instance. They exercise all five edge types, arbitrary quoted/backslash-containing values and property keys, traversal direction, removal, forbidden starting vertices, same-owner cross-tenant neighbors, and foreign mutation protection. Database transactions are rolled back after each test.
- 132 related Door authorization, tenant-scope, initialization and Neptune TLS tests passed.
- Nine repository-memory tests passed.
- Module-wide Ruff checks passed using the CI-pinned `ruff==0.15.20`.
- The Agent Context CI path filters and test command now include `personal_context/`; these tests were previously omitted.

The local database image was `neo4j:5-community@sha256:5eb12ad77fa46ab73e23df9ea1f43f5c0f2a79523435577648e046be042b9b93`. To repeat the real-query tests, use a disposable loopback database and set `PERSONAL_GRAPH_TEST_BOLT_URL=bolt://127.0.0.1:PORT` when running `pytest personal_context/tests/test_graph_security.py`. They skip without that explicit setting and require the `neo4j` Python driver. The HTTP test independently verifies Neptune's parameter envelope and exact signed payload.

This is source remediation, not deployed finding closure. The graph flag remains off by default. No live Neptune migration, IAM change or deployment was performed. Before enabling the feature, validate against the target Neptune engine and existing graph data, including any legacy multi-valued Gremlin properties, and verify the configured tenant ID format against actual integrations. Local Neo4j execution does not establish Neptune live acceptance. Raw scanner findings are retained; no suppression is added.
