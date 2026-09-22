# S09 — exec of historical definitions (Bandit B102): disposition

Work-package **S09** of the 2026-09-21 security scan (parent #5599, issue #5608).

Scanner: Semgrep, rule `tmp.gitlab.bandit.B102` ("Use of exec detected"),
1 source-rated HIGH occurrence (`original/semgrep/semgrep-results.sarif`,
`result_index=758`). Scanned commit `fb4bc620`.

## Summary

**True positive, and the underlying weakness was worse than the rule states.** The rule
objects to `exec`. The actual defect was *what* got executed: the helper selected
historical sources by **8-10 hex abbreviated Git revisions** and never verified the bytes
it received. An abbreviated revision is a lookup key resolved against whatever objects the
local checkout happens to contain — not a content identity — so the code that ran was
whatever that prefix resolved to in the checkout.

Fixed by making the pin mean the content: every source is now pinned by full 40-hex commit
ID **and** the SHA-256 of its exact bytes, verified before anything is parsed. Abbreviated
revisions and unpinned paths are refused. The executed definitions also no longer inherit
this module's globals. The `exec` itself remains — reproducing how a fixed bug behaved
requires running the historical code — carrying a narrowly scoped inline disposition.

| # | Location (scanned) | Verdict | Action taken |
|---|---|---|---|
| 758 | `docs/analysis/cli-uplift-rework/probe_historical_failures.py:34` | True positive; unverified abbreviated revisions could resolve to other content | Content-digest pinning + full-ID requirement + per-call-site namespace; scoped inline disposition on the `exec` line; 11 fixtures |

## The real weakness

The original code:

```python
def historical_functions(revision, path, names, **bindings):
    source = subprocess.run(["git", "show", f"{revision}:{path}"], ...).stdout
    nodes = [... for node in ast.parse(source).body ...]
    namespace = dict(globals(), **bindings)
    exec(compile(ast.Module(body=nodes, ...), path, "exec"), namespace)
```

Call sites passed `"3d22b4a039"`, `"18ff18db70"`, `"bf45250531"`, `"3c88c8a2"` — 8 to 10
hex characters. Three problems compounded:

1. **No content verification.** Whatever `git show` returned was parsed and executed. A
   checkout whose object store resolved the prefix differently would silently reproduce
   different code, and the probe would still print a plausible result.
2. **Abbreviated prefixes are cheap to target.** Measured single-core, pure Python, on
   this checkout — candidate commit object IDs over a fixed tree:

   ```
   rate ~= 349,033 candidate commits/sec
   prefix '3d22b4'   (24 bits): FORGED 3d22b44b915de8fe3111da177741ef228c004f2b
                                in 4,588,106 tries, 18.4s
   prefix '3d22b4a0' (32 bits): not found in 40s; expected ~205 min at this rate
   ```

   The pins in this file sat in exactly that 24-32 bit range. Anyone who can land an object
   in the checkout's object store — a fetched fork or pull-request ref is enough, no write
   access to a branch required — could aim a prefix. This is prefix-steering against
   *abbreviations*, not a SHA-1 collision.
3. **Ambient namespace.** `dict(globals(), ...)` handed the executed definitions `os`,
   `subprocess`, `sys` and `tempfile`, none of which most of the reproduced functions need.

### One mitigation that was already present, not overstated

Only module-level `def`/`class` nodes whose names were requested were kept; top-level
statements were discarded. So merely *loading* a substituted file did not run its body.
That narrowed the window but did not close it, because the retained functions are then
called (`common.save_session(...)`, `user.connect(...)`, `inference._usage_record(...)`).
The safety argument therefore rests on the digest check, not on definitions-only.

## Action taken

**Content-addressed pins, verified before compilation.** `PINNED_SOURCES` maps
`(full 40-hex commit ID, path)` to the SHA-256 of that file's exact bytes. `pinned_source()`
refuses a revision that is not full-length, refuses a `(revision, path)` pair with no
recorded digest, then hashes the bytes Git produced and refuses a mismatch. It uses
`git cat-file blob` rather than `git show` so raw object bytes are hashed with no textual
conversion. The digest — not the revision name — is what authorizes execution.

**Explicit namespace per call site.** `namespace = dict(bindings)`; module globals are not
inherited. Each call site passes only what its functions need (`Path`, `json`, `os`,
`stat`, `tempfile`, `time` for the session-writing set; `sys` for the two that warn on
stderr). `subprocess` is passed to nothing. The historical admin path's `ask()` is bound
to a helper that raises, so a future edit reaching the interactive branch fails loudly
instead of blocking on stdin.

**Documented boundary.** The module docstring states what is trusted and why, including
the honest limit of the definitions-only measure. `verify_pins()` checks all pins against
the local object store and prints digests, so rotating a pin is a defined operation.

**Scoped disposition, no global exclusion.** The `exec` line carries
`# nosec B102 # nosemgrep: tmp.gitlab.bandit.B102` with an adjacent comment explaining why
execution cannot be replaced by a safer analysis path (the audit question *is* how the old
code behaved when run) and what bounds it. `.semgrepignore`, `.github/security/semgrep.yml`
and the baseline SARIF are **unmodified**; no rule was disabled repo-wide and nothing was
baselined away. Both marker forms are present because the repo's gate treats a SARIF result
with a non-empty `suppressions` array as not-a-finding
(`.github/scripts/diff_security_findings.py`), while `.semgrepignore` records that the
GitLab-side Probe scanner does not honor inline `nosemgrep` — the Bandit-form `# nosec`
covers that path.

## Validation

Finding reproduced on the pre-fix code, then confirmed absent:

```bash
pip install bandit   # 1.9.4 in this run

git show 8c8b7d73d:docs/analysis/cli-uplift-rework/probe_historical_failures.py > /tmp/pre.py
python3 -m bandit -t B102 -f json /tmp/pre.py
# 1 result: line 34, B102 — reproduces finding 758

python3 -m bandit -t B102 -f json docs/analysis/cli-uplift-rework/probe_historical_failures.py
# 0 results
```

Suppression mechanism confirmed rather than assumed — removing only the marker, leaving
code identical, brings the finding back (1 result at the `exec` line), so the annotation is
doing the work and the construct is still visible to the scanner.

Full Bandit scan of both changed files reports 12 remaining results, **all LOW**: `B404`/
`B607` (subprocess import, `git` on PATH), `B101` (asserts — this is a probe whose
assertions *are* the counterexample checks), `B105`/`B106` (the literal `"synthetic"`
placeholder token in fixtures). No MEDIUM or HIGH. `nosec skipped: 0` for the fixtures file.

**Intended use preserved, byte-for-byte.** The probe's output is unchanged:

```bash
python3 docs/analysis/cli-uplift-rework/probe_historical_failures.py | sha256sum
# 35cd3a64b81b461a19a69473476aee2deefe2b70b23a4653c303bc075ad2f5c1  (same pre- and post-change)
```

This is also what establishes that the narrowed namespaces are sufficient: every path the
probe exercises still runs, and all five counterexamples still reproduce.

**Fixtures** — `docs/analysis/cli-uplift-rework/test_probe_trust_boundary.py`, 11 tests,
standard-library `unittest` (this helper lives under `docs/analysis/` and runs directly
from a checkout; there is no pytest suite here):

```bash
python3 docs/analysis/cli-uplift-rework/test_probe_trust_boundary.py
# Ran 11 tests — OK
```

Coverage: abbreviated revision refused; unpinned path refused; real path under a different
pinned revision refused; tampered content refused (and `historical_functions` refuses for
the same reason); absent object fails closed rather than returning empty; module globals not
inherited; all pins full-length hex; five counterexamples reproduce with the exact expected
payload; `verify_pins` reports all ok.

Each guard is **mutation-checked**, so the tests fail for their intended reason:

| Mutation | Result |
|---|---|
| `if actual != expected:` → `if False:` (digest check off) | FAILED (1) |
| `if not _FULL_OBJECT_ID.match(revision):` → `if False:` | FAILED (1) |
| `if expected is None:` → `if False:` (unpinned allowed) | FAILED (1 error) |
| `dict(bindings)` → `dict(globals(), **bindings)` | FAILED (1) |
| unmutated | OK (11) |

Tests needing the pinned objects skip with a clear message on a shallow clone instead of
failing misleadingly.

## Residual risk

The helper still executes historical code by design; that is irreducible for this audit
purpose. What changed is that execution is now gated on a verified content digest, so the
remaining trust is in the recorded digests in `PINNED_SOURCES` — reviewable values in the
repository — rather than in prefix resolution against a local object store. Rotating a pin
requires recording a new digest, which `verify_pins()` prints.

Not verified: behaviour on a repository using Git's SHA-256 object format. The digest check
is independent of Git's object-ID algorithm (it hashes file bytes with SHA-256), but the
recorded commit IDs are SHA-1 names and would need re-recording there.

## Scope

Owned: `docs/analysis/cli-uplift-rework/probe_historical_failures.py`, its new validation
fixtures, and this disposition. Not touched: any production runtime code, any module's
`src/`, `.semgrepignore`, `.github/security/semgrep.yml`, the baseline SARIF, or any global
scanner configuration. Shared release pins and global scanner disposition/baseline
reconciliation belong to **S21**.
