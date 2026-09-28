# Mediated GitHub operations — operation/request/result/error contract

Issue #5223, child of #5174 under EPIC #4191. This document is the published
contract for the mediated GitHub path: the set of operations, what each request
may carry, what each result returns, and what each error means.

It exists because #5130's merge controller will consume this endpoint, and a
consumer needs the contract without reading the implementation. **This path
supplies mediation only.** It does not schedule merges and does not decide when a
PR is mergeable; those are #5130's.

## Why mediation exists at all

A GitHub App installation token carrying `contents: write` also authorizes
`PUT /repos/{owner}/{repo}/pulls/{number}/merge`. The provider offers no narrower
grant: `contents: write` cannot be restricted to a single branch, and it cannot be
issued without the merge capability riding along. An installation token's lifetime
floor is also one hour, which routinely exceeds the grant it would serve.

So for a policy that keeps merge as a human decision,
`runtime_policy.policy_github_permissions` returns `None` — correctly, because no
token can express "may push this branch, may not merge it". Before this path, that
`None` meant the work did not happen.

Mediation removes the credential instead of weakening the gate. The worker asks
for a *typed operation*; the gateway holds the installation token, re-authorizes,
performs that one operation, and returns the result. The token never leaves the
gateway process.

## Endpoint

```
POST /internal/v1/agent/self/github-operation
```

Requires the existing agent transport — **two** proofs, on every request:

| Header | Proves |
|---|---|
| `X-Adp-Run-Credential` | which invocation and attempt is calling |
| `X-Adp-Workload-Token` | which pod is calling |

SigV4 alone is insufficient: all workers share one IAM role, so it cannot
distinguish one run from another. There is no bearer form of this authority, and
nothing is issued to the worker between requests: the operation is **synchronous**,
so there is no reference or handle to present, copy or replay. Every request
re-establishes its own authority from these two proofs plus protected records.

## Authority is derived, never asserted

Every field that decides what happens is read from protected records:

| Decided by the gateway | Read from |
|---|---|
| tenant | run credential → execution record |
| installation ID | execution record |
| repository (immutable numeric ID) | execution record |
| working branch | derived as `agent/issue-{issue}` from the protected issue number |
| accepted plan version | accepted-plan record |
| expiry | earliest of grant, policy and flow deadline |
| claim generation | read live, never accepted from the request |

A request **may not** name a repository, branch, installation, tenant, run, expiry,
HTTP method or URL. `extra="forbid"` makes an attempt to add such a field a `422`
rather than a silently ignored one. `repository` and `branch` are accepted only as
*assertions*: they are compared against the protected record and a mismatch is a
refusal, never an override. They exist so a worker whose view of its own assignment
has drifted gets refused instead of quietly writing somewhere it did not intend.

## Operations

Six, and the set is closed. Adding a member requires stating its provider
permissions explicitly; there is no permissive default.

| Operation | Requires | Provider permissions held (inside the gateway, for one call) |
|---|---|---|
| `read_repository` | assignment action | `contents: read`, `pull_requests: read`, `issues: read`, `checks: read`, `metadata: read` |
| `fetch_repository_archive` | assignment action | same as `read_repository` |
| `publish_commit` | assignment action | read + `contents: write` |
| `upsert_pull_request` | assignment action | read + `pull_requests: write` |
| `publish_review` | assignment action | read + `pull_requests: write`, `issues: write` |
| `merge_pull_request` | **`Action.MERGE` currently autonomous** | read + `contents: write`, `pull_requests: write` |

`merge_pull_request` is a separate enum member on purpose — not a mode of
`upsert_pull_request` and not a flag on it. A flag is a value a caller supplies; a
separate operation with its own required action is not reachable by a develop or
repair assignment even if that caller constructs its own request.

### Deliberately absent

No arbitrary method/URL forwarding, no workflow dispatch, no repository
settings/ruleset changes, no branch deletion, no force push, and **no operation
that returns the token**. These absences are the control, not an oversight. A
review cannot be used by a PR's own author to approve it, because a PR author
approving their own work would forge the very review a merge gate depends on.

## Requests and results

All requests are `POST` with a JSON body whose only required field is `operation`.

### `read_repository`

Present so that a *read* does not need a token either. Reads respect the same
short authorization as writes; reaching for a long-lived token merely to clone
would reintroduce the credential this path removes.

```json
{"operation": "read_repository"}
```

```json
{
  "repository": {
    "repository_id": 123456,
    "repository": "owner/repo",
    "default_branch": "main",
    "default_branch_head": "<40-hex>",
    "branch": "agent/issue-5223",
    "branch_head": "<40-hex or null>",
    "pull_request": {"number": 5232, "html_url": "...", "state": "open", "merged": false}
  },
  "branch": "agent/issue-5223",
  "idempotency_key": "<opaque>"
}
```

`branch_head` is `null` when the working branch does not exist yet, which is normal
before the first publish. The read is keyed on the immutable numeric repository ID:
if the name now resolves to a different repository, the assignment does not apply
to it — this is what catches a rename followed by a squatter taking the old name.

Note the nesting: `pull_request` is **inside** `repository`, and `repository` at the
top level is that object — while `repository` *within* it is the slug string. A
consumer that reads `pull_request` off the top level always finds nothing. It is
`null` when the assignment has no PR yet.

`state` is GitHub's own value and is only ever `open` or `closed` — **never
`merged`**. Mergedness is the separate `merged` boolean, which the gateway derives
from the provider's `merged_at`. Deciding "is this work already done?" therefore
means reading `merged`, not comparing `state`; a `state == "merged"` test cannot
ever be true, and because its failure mode is a silent duplicate run rather than an
error, nothing surfaces the mistake. Both of these cost the mediated idempotency
guard its function once already, which is why the shape is spelled out here.

### `fetch_repository_archive`

How a mediated run gets a work tree at all. `git clone` needs a credential the run
does not have, so the gateway fetches the archive with its own short-lived token and
returns the bytes. The archive is always of the assignment's own repository at a ref
the gateway resolves; there is no URL to supply.

```json
{"operation": "fetch_repository_archive", "archive_offset": 0, "archive_length": 6291456}
```

```json
{
  "commit_sha": "<40-hex>",
  "branch": "agent/issue-5223",
  "repository": "owner/repo",
  "archive_format": "tar.gz",
  "archive_total_bytes": 12665576,
  "archive_digest": "<64-hex>",
  "archive_digest_algorithm": "sha256",
  "archive_offset": 0,
  "archive_slice_bytes": 6291456,
  "archive_complete": false,
  "archive_base64": "<base64>",
  "idempotency_key": "<opaque>"
}
```

**The result is sliced because the deployed transport requires it.** The endpoint is
served by a REST API Gateway, whose 10 MB response payload limit is a hard service
quota that cannot be raised, and base64 in JSON costs 4/3 of the raw bytes — so a
single response can carry at most ~7.5 MB of archive. `ARCHIVE_SLICE_BYTES` is 6 MiB
(~8.39 MB encoded) to leave room for the envelope. A caller repeats the operation at
successive `archive_offset` values until `archive_complete` is true. The same edge
imposes a 29 s integration timeout, which is why the provider fetch is bounded well
below it rather than at a leisurely several minutes.

`archive_digest` is the SHA-256 of the **whole** archive and `archive_total_bytes`
its full length; both are returned with every slice, and a caller must check they do
not change across slices. This is what makes a multi-call read safe: `git archive`
output is *not* byte-identical across fetches of the same commit (gzip embeds
metadata), so `commit_sha` alone cannot prove the slices came from one archive. A
digest or total that changes mid-transfer means the underlying archive moved and the
partial result must be discarded, not stitched. The reassembled bytes are verified
against the digest before use, so a truncated or spliced transfer fails loudly
instead of yielding a subtly wrong work tree.

Slicing costs re-fetching: an N-byte archive is fetched `ceil(N / slice)` times
inside the gateway, so cost is quadratic in repository size. `MAX_ARCHIVE_BYTES` is
32 MiB (~6 fetches) — a deliberate bound, not a tuning knob. The alternative,
staging the archive in object storage and returning a pre-signed URL, would avoid
the re-fetch but needs a bucket and worker-side permissions that are frozen by
#5195/#5210; slicing keeps the existing route, signing and permission set unchanged.

### `publish_commit`

```json
{
  "operation": "publish_commit",
  "message": "<commit message>",
  "expected_head": "<40-hex or null>",
  "changes": [
    {"path": "a/b.py", "content_base64": "<base64>", "mode": "100644", "deleted": false}
  ]
}
```

Content is base64 because the helper publishes **actual local git changes**, which
include binary files and bytes that are not valid UTF-8. Encoding at the boundary
means the platform never guesses a text encoding for content it is only
transporting. `mode` carries file-mode changes (`100644` regular, `100755`
executable, `120000` symlink); gitlinks (`160000`) are refused rather than
translated, because a submodule imports code nothing reviewing the change has seen.

Bounds: 5 MiB per blob, 6 MiB of content summed across the commit, 500 files per
commit. Published via the GitHub tree/commit/ref APIs with validated base ancestry
and `force: false`.

Those first two numbers are set by the transport, not by taste. A publish request
travels the same REST API Gateway as an archive response, and its 10 MB payload
quota is a hard service limit in **both** directions; base64 costs 4/3, so 5 MiB
encodes to ~6.99 MB and the 6 MiB aggregate to ~8.39 MB, each leaving room for the
envelope. The aggregate bound exists because the per-file cap cannot express it:
500 files each just inside 5 MiB is ~2.5 GB. Both are enforced worker-side too, so
an oversized commit is reported with the offending size rather than surfacing as
the edge dropping the request.

Those field caps are not the final transport bound. The worker also measures the
complete serialized JSON request and refuses anything above 9 MiB before signing
or opening a connection. Permitted paths and the commit message can expand under
JSON escaping, so content that fits by itself does not prove the whole body fits
REST API Gateway's hard 10,000,000-byte request quota. The remaining margin is
deliberate edge/envelope headroom.

`expected_head` is **required when the assigned branch already exists** — it is the
commit the change was prepared against. Omitting it is refused with a `409` rather
than treated as "publish anyway": without it the ref update cannot be conditional,
so a commit pushed by anything else between preparation and publication would be
silently overwritten. `null` is only valid for creating the branch.

```json
{
  "commit_sha": "<40-hex>",
  "parent_sha": "<40-hex>",
  "branch": "agent/issue-5223",
  "idempotency_key": "<opaque>"
}
```

The worker helper remembers the last confirmed remote head and the local tree
published with it under the repository's git directory. Its next call compares
against that tree, including changes already committed locally. Before the first
publication it uses bootstrap's upstream tracking ref. Separate helper processes
therefore continue the same checkpoint sequence; a different writer advancing the
remote branch still produces a conflict. A refused or unavailable operation does
not advance the local publication receipt.

### `upsert_pull_request`

```json
{"operation": "upsert_pull_request", "title": "<title>", "body": "<body>"}
```

Creates the assigned branch's PR or updates it if one already exists. The base and
head are derived, so this cannot open a PR from or into a branch other than the
assigned one.

```json
{"pull_request": {"number": 5232, "html_url": "...", "state": "open"}, "idempotency_key": "<opaque>"}
```

### `publish_review`

```json
{"operation": "publish_review", "pull_number": 5232, "body": "<body>", "review_event": "COMMENT"}
```

The named `pull_number` is **verified to be this assignment's own pull request**:
its head must be the derived working branch and its base the recorded default
branch. A caller naming any other PR is refused. `pull_number` is therefore an
assertion like `repository` and `branch` — it selects among the assignment's own
work, it does not widen what the operation can touch.

`APPROVE` is accepted by the signature but refused when the PR is on the calling
run's own assigned branch. Note the composition: because ownership is now enforced,
every PR reachable through mediation *is* on the assignment's own branch, so
`APPROVE` is unreachable in practice — self-approval is refused structurally rather
than by a check that could be bypassed. `COMMENT` and `REQUEST_CHANGES` are
unaffected.

### `merge_pull_request`

Same shape as `publish_review` minus the event, plus a required `expected_head` (the
commit the PR was reviewed at). Refused unless `Action.MERGE` is currently
autonomous under the accepted policy. A develop or repair assignment cannot reach
it, and the named PR must be the assignment's own.

The gate is the *policy*, not the assignment's action: an assignment never carries
`Action.MERGE` (the runtime derives only develop/repair/review/evaluate), so merge
is admitted purely by an owner ungating it. That is deliberate — a merge gate that
held because the code path was dead could not be reopened by the owner who set it.

## Idempotency

Each result carries an `idempotency_key` bound to **both** the request hash and the
assignment. Two different requests therefore cannot reuse one key, and a key from
another assignment does not apply.

Be precise about what carries the safety here, because the key looks like more than
it is: **there is no durable key ledger.** The gateway does not persist the key and
does not consult it, so the key alone does not refuse a redelivered request. What
makes a retry after a provider timeout safe is **reconciliation** — the gateway looks
up whether the intended commit or PR actually landed and only then reports, so a
retry cannot publish the same change twice. The key is a stable name for one intended
effect, which is what a durable record could later key on; adding that record is a
separate design decision and is not implemented today.

Reconciliation identifies a landed commit by the **tree and parent** the gateway had
already built, not by its commit message. A message match alone is not evidence:
two runs, or a retry of the same run, can produce identical messages over different
content. When the gateway cannot prove which commit landed, the outcome stays
**unknown** and the `503` surfaces unchanged rather than reporting a success it
cannot substantiate — a worker that must reconcile is strictly safer than one told
its change landed when it may not have.

## Errors

| Status | Meaning | What a consumer should do |
|---|---|---|
| `409` | The assigned branch or PR moved (expected-old-head mismatch) | **Visible conflict.** Fetch, rebase, retry. Reachable only after full authorization. |
| `503` | Provider or gateway unavailable | Retry is reasonable; reconciliation has already run, so a retry cannot double-publish. |
| `404` | Any authorization refusal | Do **not** retry unchanged. The reason is logged, never returned. |
| `422` | Malformed body, or a field that does not exist in the contract | Fix the caller. |

`404` is one shape for every authorization failure on purpose: a caller able to
distinguish "not authorized" from "does not exist" learns what exists. A `409` is
the deliberate exception, because a conflict is information the worker needs in
order to reconcile, and it is only reachable once authorization has fully passed.

## When authorization runs

Immediately before **every** provider mutation — not once per request. That
includes each retry and again after a bounded upload completes. The window between
"authorized" and "effect" is where a revoked grant, a released claim, a superseded
plan or a withdrawn human acceptance would otherwise still land a write.

Revocation blocks *subsequent* effects. This path makes **no promise to undo** a
provider action that already completed, because it cannot.

## Repository-automation content

A proposed change that could itself perform a gated action through repository
automation — a workflow definition, a composite action — is refused unless both
`Action.MERGE` and `Action.DEPLOY` are separately autonomous.

Branch naming is deliberately **not** accepted as a bound here.
`pull_request_target`, `workflow_run` and a later merge all run a definition from a
ref that the branch name does not constrain, so "it is only on the agent branch" is
not evidence that the definition cannot execute with more authority.

## Withholding the token, and keeping it withheld

Mediation's guarantee is that no merge-capable credential is reachable from the
agent process. Two halves make that true, and both are necessary:

* **At startup**, `entrypoint._withhold_write_token` removes every token variable
  (`GITHUB_TOKEN`, `GH_TOKEN`, `GH_APP_TOKEN`, the private key), removes `GIT_ASKPASS`
  and `ADP_TOKEN_FILE`, and deletes the on-disk token file — the shell helpers
  (`gh-wrapper`, `git-askpass-helper`) *default* that path, so unsetting the variable
  alone would not stop them reading it.
* **Across refresh**, `isMediatedRun` (`agent/src/mediated-github-config.ts`) refuses
  every mint and every publication for the life of the process.

The second half is not optional. The worker's Node process re-mints tokens on a
5-minute timer, on a 401, and on demand; those gates originally tested only
`ADP_TOKEN_MODE === 'pat'`, and withholding sets it to `"mediated"`. Public
identifiers (`GH_APP_ID`, `GH_APP_INSTALLATION_ID`, `REPO_OWNER`) survive withholding
by design, and broker mode is true for the policy-bearing cohort on
`ADP_AGENT_AUTHORITY_ENABLED` alone — so every precondition for a re-mint outlives
the startup strip. The guard is applied where tokens are *minted* and at
`publishToken`, the single write all restore paths converge on, so a new refresh path
is guarded by construction rather than by remembering to add a check.

A mediated run therefore reports "no token to refresh" and proceeds tokenless; it
does not fail to start. Anything calling `getRuntimeGitHubToken` in a mediated run
gets a refusal naming the mediated path, because such a caller has a bug to fix
rather than a transient failure to retry.

## Activation

Dormant until switched on. The worker helper checks
`ADP_MEDIATED_GITHUB_ENABLED`; with it unset, behavior is unchanged. That flag is
the only activation control — **no key or secret needs seeding for this route.**

An earlier draft of this document said `AGENT_GITHUB_OPERATION_KEY` had to be
seeded or "reference minting fails closed". That described a signed-reference
exchange that was never wired: the route is synchronous and reads no such key, so
the claim named a gate that did not exist. Believing it would have been the
dangerous direction of error — an operator could conclude the route was inert
because a secret was absent, when in fact the flag alone governs it. The unused
helpers have been removed rather than left to imply live enforcement.

Permission-map/registry and scoped IAM integration for new internal routes go
through #5195/#5210. Merging this code is not an IAM apply and not an activation.

## For #5130

The merge controller consumes `merge_pull_request` with a run whose accepted policy
makes `Action.MERGE` autonomous. Everything it needs is above: the operation name,
its required action, the request shape, the result shape, and the error semantics —
in particular that `409` means "reconcile and retry" and `404` means "do not retry
unchanged". This path will not merge anything on its own.
