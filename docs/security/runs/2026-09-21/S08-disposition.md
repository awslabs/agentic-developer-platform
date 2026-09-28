# S08 — Git blob SHA-1 (Bandit B324): disposition

Work-package **S08** of the 2026-09-21 security scan (parent #5599, issue #5607).

Scanner: Bandit 1.7.9, rule `B324` ("Use of weak SHA1 hash for security"),
1 source-rated HIGH occurrence. Scanned commit `fb4bc620`.

## Summary

The finding is a **true positive for the rule's pattern and a false positive for the
weakness it names**: the flagged call computes **Git's blob object ID**, not a security
digest. The hash algorithm is fixed by Git's on-disk object format, so replacing it would
break verification rather than strengthen it. Resolved with the explicit non-security
annotation the runtime provides (`usedforsecurity=False`) — digest unchanged — plus tests
that prove the surrounding verification still rejects mismatched content.

| # | Location (scanned) | Verdict | Action taken |
|---|---|---|---|
| 8712 | `modules/gateway/src/orchestration/deployment_workflow_provider.py:162` | Not a weak-hash vulnerability — Git object identity, no authorization role | `usedforsecurity=False` + explanatory comment; 2 tests added |
| — | `modules/gateway/src/orchestration/repository_evaluation_provider.py:155` | Git object identity; **not in the scan** (file post-dates `fb4bc620`) | Same annotation plus full-content cross-revision comparison and a collision-negative test |

## Why SHA-1 here is not a choice

Git names a blob by hashing a framed form of its contents:

```
object_id = SHA-1("blob " + <byte length> + "\0" + <contents>)
```

That expression *is* the object ID. The code fetches a workflow definition through the
GitHub Contents API and recomputes the ID to confirm the bytes it received are the object
the API named in its `sha` field. Verified equivalence with Git itself:

```bash
printf 'name: Deploy\n' > /tmp/blobtest.txt
git hash-object /tmp/blobtest.txt
# 79a2a729360dd745a81ae71c5a1038778758514c

python3 -c "
import hashlib
c = open('/tmp/blobtest.txt','rb').read()
print(hashlib.sha1(b'blob '+str(len(c)).encode()+b'\0'+c).hexdigest())
print(hashlib.sha1(b'blob '+str(len(c)).encode()+b'\0'+c, usedforsecurity=False).hexdigest())
"
# 79a2a729360dd745a81ae71c5a1038778758514c
# 79a2a729360dd745a81ae71c5a1038778758514c
```

A SHA-256 digest would equal no value GitHub ever reports, so every
workflow-definition check would fail closed and deployments would stop. Git's own
SHA-256 object format is a repository-wide storage choice, not something a client can
elect per request; the surrounding revisions (`definition_revision`, `source_revision`,
`dispatch_revision`) are themselves Git SHA-1 names, so the protocol fixes the algorithm.

## The hash is not an authorization credential

This is the acceptance question that decides annotate-vs-redesign. The computed value has
two roles, neither of which grants authority:

1. **Transport consistency.** `blob != record.get("sha")` compares the recomputed ID
   against the `sha` in the *same* API response — a self-consistency check on one
   response, not a trust decision. A forged response could supply a matching pair, which
   is why it is not treated as authentication.
2. **Change-detection pin.** `blob_sha` is recorded and later re-compared in
   `deployment_workflows.perform` (`definition_keys`) to detect a definition changing
   between approval and dispatch.

Authorization comes from separate, independent controls: the approved
`definition_revision` pin, the allowed-input allow-list, the repository-identity check
(`provider_repository_id`), and a short-lived scoped installation token. Transport
integrity is TLS to `api.github.com`.

Critically, the cross-revision equality check does **not** rely on the digest alone:

```python
blobs.append((blob, content))
if any(blob != blobs[0] for blob in blobs[1:]):
    raise CycleBlockedError("deployment_workflow_revision_mismatch")
```

The list holds `(digest, content)` tuples, so the comparison includes the full definition
bytes. A SHA-1 collision alone therefore cannot substitute a different workflow
definition across revisions — the strongest available argument that the known
collision weakness in SHA-1 is not exploitable at this call site.

## Action taken

`usedforsecurity=False` is Python's documented per-call marker for a non-security hash
(CPython 3.9+; this project declares `requires-python = ">=3.12"` and the scanner job
runs 3.12, so it is available everywhere this code runs). It is exactly the remedy
Bandit's own message recommends, is scoped to the two lines that need it, and leaves the
digest byte-identical as shown above. An adjacent comment records why the algorithm is
fixed and that the value carries no authorization weight. The repository-evaluation
provider also compares `(blob, content)` tuples across the approved and source revisions,
so its annotated digest is not the sole workflow change-detection check.

**No rule was disabled, no `# nosec` was used, no baseline was refreshed, and
`.github/security/.banditrc` was not modified.** The Bandit JSON reports
`nosec skipped: 0`, confirming nothing was suppressed.

## Validation

Finding reproduced on the pre-fix code, then confirmed absent after — same pinned
scanner version and same repository config as the scan workflow:

```bash
pip install "bandit[sarif]==1.7.9"

git show fb4bc620d065a124f1c73f53c3956159e60b4d36:modules/gateway/src/orchestration/deployment_workflow_provider.py > /tmp/pre.py
bandit -q --ini .github/security/.banditrc -f json /tmp/pre.py
# 1 result: line 162, B324, HIGH — reproduces finding 8712

bandit -q --ini .github/security/.banditrc -f json \
  modules/gateway/src/orchestration/deployment_workflow_provider.py \
  modules/gateway/src/orchestration/repository_evaluation_provider.py
# 0 results; SEVERITY.HIGH: 0; nosec skipped: 0
```

Tests — the focused cases fail if the verification they cover is removed:

```bash
cd modules/gateway
ruff check src/ tests/ && ruff format --check src/ tests/
python3 -m pytest tests/orchestration/ -q
```

- `test_definition_bytes_must_hash_to_the_git_object_id_the_provider_named` — the API
  returns a `sha` that does not match the bytes it served; the cycle must block with
  `deployment_workflow_blob_mismatch`. Replacing the guard with `if False:` fails this
  test, so it exercises the real check rather than asserting a constant.
- `test_definition_blob_sha_is_the_git_blob_object_id_of_the_definition` — recomputes the
  Git object ID independently of the provider, pinning the formula so a future edit
  cannot silently change the algorithm.
- `test_workflow_definition_requires_matching_content_even_when_blob_ids_match` — supplies
  equal digest values with different workflow bytes and requires the evaluation to block.
- Mismatched content across revisions remains rejected by the pre-existing
  `test_changed_workflow_bytes_are_not_the_approved_definition`.

## Scope

Owned: the Git blob object-ID computation at the two locations above, its tests, and this
disposition. Not touched: gateway auth, admin, budget or deployment-rollout behaviour;
`.github/security/.banditrc` or any global Bandit configuration; shared release pins and
global scanner disposition/baseline reconciliation, which belong to S21.

The second location is outside the issue's listed file. It is included because it is the
identical construct in the same package and would otherwise surface as a new HIGH with
the same root cause on the next scan; it is called out in the PR so a reviewer can ask
for it to be dropped.
