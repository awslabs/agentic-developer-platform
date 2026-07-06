# Learnings: Issue #2437 — `secure` MCP Verb Product Enrichment

**Date:** 2026-06-30
**Agent:** @agent-product
**Issue:** #2437
**PR:** #2440
**Type:** Product story / EPIC enrichment

## What Worked

1. **Deep infrastructure research before writing** — Understanding the exact data models (NormalizedVulnerability fields, dependencies table schema, Neptune query patterns) meant the I/O contract could reference real field names, real query capabilities, and real degradation modes instead of aspirational hand-waving.

2. **Parallel exploration agents** — Launching 3 Explore agents simultaneously (MCP verbs, vuln scanner, Neptune call graph) gathered comprehensive context in one round-trip instead of serial reads.

3. **Grounding in existing patterns** — The product story specifies the exact same ACL, dispatch, and MCP registration patterns used by the 6 existing verbs. This makes architect/developer work concrete: "add an elif here, follow this decorator pattern there."

4. **Reachability as 4 discrete levels** (present → imported → called → reachable) maps directly to what the existing infrastructure can actually determine:
   - Level 0: SBOM + dependencies table (already indexed)
   - Level 1: Zoekt import search (already available)
   - Level 2: Neptune CALLS edge exists (already queryable)
   - Level 3: Neptune bounded 4-hop path (already implemented for `impact`)

## Key Technical Decisions

1. **Sub-actions within one verb, not separate verbs** — identify/plan/verify all operate on the same SBOM↔vuln↔callgraph join. Separating them would duplicate the join logic and fragment the tool surface. This matches how `browse` uses `action` parameter for different operations.

2. **Fail-safe reachability** — Default to `reachable: true` when can't determine. Rationale: false positive (unnecessary fix) is cheap; false negative (missed vulnerability) is expensive. This matches the existing test fixture pattern in `test_vuln_loop.py` line 48: `return self._reachability.get(repo_id, {}).get(package, True)`.

3. **200 OK for "not found" cases** — Following existing verb pattern: unknown CVE returns empty findings with metadata, NOT a 404. ACL denial returns empty findings. This is consistent with how `impact` returns `verdict: "symbol_not_found"` rather than an error.

4. **Priority as composite score** — Simple multiplicative model (severity × reachability × fix_availability) is transparent, tunable, and auditable. Avoids ML/opaque scoring that would be hard to explain in compliance contexts.

## Gotchas / Non-Obvious Findings

1. **Neptune is NOT always available** — The system has a 5-second timeout fallback to S3 code-index.json. The `secure` verb MUST handle degraded mode gracefully (levels 2-3 unavailable). The existing `impact` verb already does this, but it returns less useful data in fallback mode.

2. **The `dependencies` table uses purl (Package URL) as the coordination key** — e.g., `pkg:npm/lodash@4.17.0`. Any join between the `vulnerabilities` table (which stores `package` as plain name) and `dependencies` table requires matching on the package name extracted from the purl. This is a data-join complexity the architect needs to address.

3. **SBOM is generated at ingestion time, NOT on-demand** — The CycloneDX JSON in S3 reflects the state at last `ingest-repo.py` run. There's no mechanism to trigger a fresh SBOM generation from within a verb handler. The `verify` action must check the last-indexed SBOM and report its age.

4. **Cross-repo impact uses SCIP symbol_id (moniker), not name+file** — This is the only way to avoid false edges. The `secure` verb's reachability check for Level 3 should use the same moniker-based resolution when determining if a vulnerable package's symbol is reachable.

5. **The vuln scanner pipeline exists but isn't wired into production ingestion yet** — `test_vuln_loop.py` tests the triage logic with fixtures, but the actual `ingest-repo.py` pipeline doesn't call the scanner. The `vulnerabilities` table needs to be populated before `secure` can return real findings.

## File Locations (for future agents)

| Component | Path |
|-----------|------|
| Product story (this work) | `docs/agent-context/design-2437-secure-verb-product-story.md` |
| Existing verb definitions | `modules/agent-context/door/server.py` lines 48-125 |
| MCP tool registration | `modules/agent-context/door/mcp_app.py` lines 151+ |
| Verb dispatch router | `modules/agent-context/door/server.py` line 423 (`_dispatch_tool`) |
| Neptune client | `modules/agent-context/door/neptune_client.py` |
| Vuln normalization | `modules/agent-context/pipeline/vuln_scanner/normalize.py` |
| SBOM parser | `modules/agent-context/images/ingestion/sbom_parser.py` |
| Dependencies table schema | `modules/agent-context/alembic/versions/001_knowledge_layer_schema.py` lines 82-96 |
| Triage logic (test) | `modules/agent-context/tests/unit/test_vuln_loop.py` |
| Product vision (context) | `docs/agent-context/knowledge-layer-product-vision.md` |

## Recommendations

1. **Architect should address the purl↔package join ambiguity first** — The vulnerabilities table stores `package` (plain name like "lodash") while dependencies stores `package_coordinate` (full purl like "pkg:npm/lodash@4.17.0"). The data-join layer (child issue #1) needs to handle ecosystem-qualified matching.

2. **Start with `action=identify` only** — Plan and verify are useful but depend on identify working correctly. Ship identify first, add plan/verify as follow-up.

3. **Test with the existing fixtures** — `tests/test_vuln_normalize.py` has real OSV/Trivy output fixtures. Use them to validate the join logic.

4. **The `git push` may fail if not on the branch** — Use `--head agent/issue-2437` flag with `gh pr create` when the local checkout doesn't match (learned from the push error in this session).
