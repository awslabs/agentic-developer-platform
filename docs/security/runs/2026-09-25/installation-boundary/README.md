# GitHub installation boundary review — #6119

The legacy local-token refresh path could select an unrelated tenant's GitHub
App installation when the requested owner was missing, malformed, or had no
resolvable installation. It fetched `/app/installations` and chose the first
entry. The shared resolver also followed redirects before checking the final
origin; that check rejected the response too late to prevent destination access.

The resolver now returns only the trusted bootstrap installation ID or the
positive numeric ID from an exact owner-specific org/user endpoint. An absent,
malformed or unresolved owner returns null. Fetch refuses redirects before a
second destination receives a request, including same-origin redirects. A renamed
owner must therefore be supplied under its current name or by its explicit
bootstrap installation ID. No App-wide fallback remains.

Caller review found two production consumers: `agent-worker.ts`'s
`refreshAppToken()` wrapper and `utils/ghPost.ts`'s `refreshGitHubToken()`. Both
already skip minting when resolution returns null. Neither exposes resolver
options to task/model input. The explicit ID path remains the existing worker
bootstrap contract: `entrypoint.py` reads the selected installation from the
parsed queue envelope's `source_ref`, uses that installation during bootstrap,
and exports `GH_APP_INSTALLATION_ID`. This fix adds numeric validation without
introducing another authority source. Mediated, broker and PAT paths retain
their existing early returns. Ordinary missing-context refresh retains the
existing token; it cannot replace it with an unrelated tenant's token.

`review.json` preserves exact original selectors `run=0|ri=2998` and
`run=0|ri=2999`, including native rule severity, SARIF level, suppression state
and original candidate links. Their original locations are lines 114 and 127 of
the frozen resolver. Both have native `CRITICAL` rule metadata and neither is
suppressed; this is not a newly assigned CVSS score. The original 13,139-selector
manifest is unchanged. This review covers two occurrences, not the whole story.

The pinned Semgrep 1.80.0 rule was extracted from the original local rule bundle
and run against exact `git show COMMIT:path` bytes. The before scan reproduces two
observations. The after scan retains one at the owner lookup and has zero scan
errors. That residual taint-rule observation is recorded with the tested boundary;
no suppression or claim of a clean global scan was introduced. The App-wide call
has been removed. Source commit and byte/artifact hashes are in the receipt.

Validation: four Jest suites pass 62 tests, including the actual `ghPost` refresh
consumer. Against the previous implementation, the same tests produced 37
failures and 25 passes. Native-fetch tests use two synthetic loopback servers and
HTTP 301/302/303/307/308 responses: the previous implementation contacted the
redirect target three times per status, while the fix makes zero target requests.
No real GitHub calls, candidate credentials, human sessions or workloads were used.

Run the tests from `modules/agent-factory/agent`:

```bash
npm ci --include=dev --ignore-scripts
npx jest --runInBand --runTestsByPath \
  src/utils/installation.test.ts src/utils/installation-redirect.test.ts \
  src/utils/ghPost.test.ts src/agent-worker-installation-resolution.test.ts
```

The dedicated GitHub Installation Boundary Tests job runs this command for agent
source changes. #6119 remains open for the rest of its original scope.
