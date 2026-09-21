# S05 — Fixed-origin GitHub API SSRF alerts: disposition

Work-package **S05** of the 2026-09-21 security scan (parent #5599, issue #5604).

Scanner: Semgrep, rule `gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf`,
3 source-rated CRITICAL occurrences. Scanned commit `fb4bc620`.

## Summary

All three alerts are **false positives for SSRF**: both call sites build their URL
from a string constant origin, so no input can redirect the request to another host.
Two of the three had a *narrower* real weakness, and both are now closed.

| # | Location (scanned) | SSRF verdict | Action taken |
|---|---|---|---|
| 2347 | `modules/agent-factory/agent/src/utils/installation.ts:61` | False positive — origin is constant | Hardened anyway: owner-login validation (blocked in-API path traversal) + response-origin check |
| 2348 | `modules/agent-factory/agent/src/utils/installation.ts:74` | False positive — URL is fully literal, no interpolation | Response-origin check added; no other change needed |
| 2418 | `modules/agent-factory/codex-reviewer/src/github.ts:114` | False positive — not reachable; every caller passes a `/`-rooted path | Hardened: rooted-path invariant enforced + response-origin check |

## Why the rule fires, and why it is not SSRF here

`javascript-ssrf-rule-node_ssrf` is a **taint-mode** rule whose only `pattern-sources`
are Express-style handlers:

```yaml
pattern-sources:
- patterns:
  - focus-metavariable: $REQ
  - pattern: function ($REQ, $RES, ...) {...}
- patterns:
  - focus-metavariable: $REQ
  - pattern: function $FUNC($REQ, $RES, ...) {...}
```

Neither file contains such a handler — there is no Express request object anywhere in
either module. Two independent checks confirm the rule matched the **sink only**, with
no source and therefore no dataflow:

1. No `function(req, res, ...)` form exists in either file.
2. Every SARIF result has **no `codeFlows`** entry, i.e. Semgrep proved no
   source-to-sink path; it reported the bare `fetch` call.

The origin at both sites is a string constant (`https://api.github.com`), so the
defining property of SSRF — an attacker choosing the destination host — is absent.

## What was genuinely wrong (and is now fixed)

The SSRF label was wrong, but reviewing the sites surfaced two real, narrower issues.

**1. In-API path traversal at `installation.ts:61`.** `owner` was interpolated into a
path segment with no validation. Because `..` is resolved by URL normalization, a
hostile owner reached a *different GitHub endpoint* (not a different host):

```
https://api.github.com/orgs/../../installation  ->  https://api.github.com/installation
```

The value derives from a GitHub-signed webhook's repository name, so it was not
attacker-controlled in practice — but nothing at the function asserted that, and it
arrives via the `REPO_OWNER` environment variable. Now validated against the charset
GitHub actually issues for owner logins (`^[A-Za-z0-9-]{1,39}$`); a malformed value
warns and takes the pre-existing no-owner fallback, so no operation is lost.

**2. Unwritten leading-slash assumption at `github.ts:114`.** The URL is built as
`` `https://api.github.com${path}` ``, which only stays on that origin while `path`
starts with `/`. All seven callers do, and the method is private, so this was not
reachable — but every request carries a GitHub installation token, so a single future
caller omitting the slash would create a genuine credential-bearing SSRF. Verified
escapes:

| `path` | Resulting origin |
|---|---|
| `@evil.com/x` | `https://evil.com` |
| `.evil.com/x` | `https://api.github.com.evil.com` |
| `:8080/x` | `https://api.github.com:8080` |
| `//evil.com/x` | `https://evil.com` (protocol-relative) |

The invariant is now enforced instead of assumed. It rejects nothing any current
caller sends.

**Redirect behavior (all three sites).** Tested against a local redirect chain on Node
v24.21.0: `fetch` follows a cross-origin redirect but **strips the `Authorization`
header** when the origin changes, so neither the App JWT nor the installation token
leaks. The residual risk is the *response body*, which at these sites supplies the
installation id used to mint a token and the check state that gates a merge. Each
response is now confirmed to have come from `api.github.com` via its final URL.
Same-origin redirects — which GitHub issues for renamed orgs, users and repositories —
keep working, so no required GitHub operation is affected.

## Validation

Ruleset and version match the scan workflow (`.github/workflows/security-scan.yml`):
semgrep 1.177.0, `r/all` plus `.github/security/semgrep.yml`.

```bash
curl -fsSL https://semgrep.dev/c/r/all -o /tmp/semgrep-rules-all.yaml
semgrep scan --config /tmp/semgrep-rules-all.yaml --config .github/security/semgrep.yml \
  --sarif --output /tmp/semgrep-after.sarif --metrics=off \
  modules/agent-factory/agent/src/utils/installation.ts \
  modules/agent-factory/codex-reviewer/src/github.ts
```

Result: the same 3 `node_ssrf` findings, no new findings. The rule matches the `fetch`
sink unconditionally, so it still reports these lines; that is expected and is why the
disposition is documented here rather than suppressed. **No inline annotation, rule
disable, or baseline change was used** — the findings remain visible.

An earlier iteration of the rooted-path guard used a regex and drew two *new* alerts
(`regex_dos` under both `gitlab.nodejs_scan` and `ajinabraham.njsscan`). The pattern was
anchored and backtrack-free, so not actually vulnerable; it was rewritten with
`startsWith` comparisons so the change adds no alerts for a reviewer to triage.

Tests (both guards verified by mutation — deliberately breaking each guard fails tests):

```bash
cd modules/agent-factory/agent && npx tsc --noEmit && npx jest src/utils/
# 295 passed. Relaxing the owner charset fails 10 tests; defeating the origin check fails 2.

cd modules/agent-factory/codex-reviewer && npm test
# 27 passed. Removing the path guard fails 1 test; removing the origin check fails 1.
```

Pre-existing on main, unrelated to this change (identical 7 failures on the base commit):
`beads-s3-config`, `complex-task-chat/ag-ui-events`, `github-comments`,
`knowledge-layer-config`, `lib/provenanceClient`.

## Scope

Owned: outbound GitHub URL safety at the three listed locations, and scanner
applicability. Not touched: agent-wide URL guards (S04), npm lockfiles (S06), gateway
installation-authorization semantics (S11), shared release pins and global scanner
disposition (S21).
