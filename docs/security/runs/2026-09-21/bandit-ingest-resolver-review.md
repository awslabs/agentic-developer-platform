# Ingest identity resolver transport boundary

Original selector `bandit|bandit-results.sarif|run=0|ri=652`, MEDIUM B310,
`modules/agent-factory/gateway/lambdas/ingest/user_resolver.py` line 139, frozen
source `b1d0894c17c686f27c2747057dead0b5a0e6b17e`, remains owned by #6108.

The configured resolver endpoint receives `X-Internal-Api-Key` and user identity
JSON. The old urllib default followed 301/302/303 redirects to another origin and
forwarded the custom API-key header. The URL begins with operator configuration,
not a request parameter; this review does not claim arbitrary initial endpoint
selection by an unauthenticated caller.

The source fix validates that configuration as HTTP(S), refuses userinfo,
query/fragment components and malformed ports, and uses a per-request opener
that refuses redirects without installing a global urllib handler. All redirects
are refused because identity resolution has a fixed endpoint. Internal HTTP is
retained for the existing service address; this change does not claim transport
encryption. The five-second timeout, successful response/cache behavior and 404
magic-link response contract remain intact. Errors log static text/status,
without response reasons or raw exceptions that could carry sensitive data.

The new transport suite uses disposable loopback HTTP servers and a synthetic
key, never live endpoints or credentials. It covers 301/302/303/307/308 and
same-origin redirects, invalid URL configuration, successful/cache behavior,
404 magic links, timeout and error log redaction. Against unchanged source the
301/302/303 cases fail by accepting the destination's identity response; with
the fix no redirected server receives the key. Existing handler integration
fixtures now patch the dedicated resolver transport after module reload.
All 28 resolver tests and all 478 gateway Lambda tests pass; lint passes for
the changed Python files and formatting passes for production/new-test code.

The inventory preserves all 1470 original identities/severities. Only this
selector is marked `fixed-source-runtime-open`; no test-path classification or
unrelated finding is changed. No ingest Lambda deployment, gateway/tick write,
cluster mutation, or live identity acceptance was performed. Runtime acceptance
remains required before this boundary is considered deployed.
