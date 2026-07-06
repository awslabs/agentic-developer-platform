# Issue #2435 — LiteLLMEmbeddingClient wrong kwarg (base_url → proxy_url)

## Date: 2026-06-30
## PR: #2439
## Issue: #2435 (part of EPIC #2400 via #2426)

## What Happened
The `experience` and `remember` MCP verbs returned "Experience tool not available" in production. The root cause was a simple typo-style bug: `LiteLLMEmbeddingClient(base_url=config.litellm_url)` should be `LiteLLMEmbeddingClient(proxy_url=config.litellm_url)`.

## Why It Was Hard to Find
1. The broad `except Exception` in `_init_experience_tool()` caught the `TypeError` silently
2. The warning log was generic ("Failed to initialize experience tool") without showing the specific error
3. Unit tests for the server module require heavy dependencies (FastAPI, boto3, etc.) making local testing harder
4. The kwarg name `base_url` is common in HTTP clients (e.g., httpx) so it "looks right" at first glance

## Fix
Single character change: `base_url=` → `proxy_url=` at `door/server.py:257`

## Regression Tests Added
- `test_litellm_client_uses_proxy_url_kwarg` — verifies constructor accepts proxy_url
- `test_litellm_client_rejects_base_url_kwarg` — verifies base_url is NOT accepted
- `test_constructor_signature_has_proxy_url` — introspects the signature
- `test_server_passes_proxy_url_not_base_url` — AST-level check on door/server.py call site

The AST-level test is notable: it doesn't need to import door.server (which has heavy deps), but still validates the correct kwarg is used at the call site.

## Key Decisions
- Used AST-based testing instead of mock-patching door.server — avoids dependency on FastAPI/boto3 in the test environment
- Did NOT attempt to fix #2436 (zoekt Service ClusterIP routing) — that's a Kubernetes networking issue, not a code/manifest defect. The Service selector correctly matches pod labels.

## Relevant Files
- `modules/agent-context/door/server.py` — `_init_experience_tool()` function
- `modules/agent-context/personal_context/embeddings.py` — `LiteLLMEmbeddingClient` class definition
- `modules/agent-context/door/config.py` — `config.litellm_url` (reads from `LLM_BASE_URL` env var)
- `modules/agent-context/manifests/agent-context-configmap.yaml` — sets `LLM_BASE_URL` in prod

## Gotchas
- `personal_context/embeddings.py` uses `proxy_url` because it wraps a LiteLLM **proxy** endpoint (not a direct LLM API). The naming is intentional to distinguish from raw model endpoints.
- The configmap sets `LLM_BASE_URL` (generic name) but the client's parameter is `proxy_url` (specific to the proxy pattern). This naming mismatch is the original source of confusion.
