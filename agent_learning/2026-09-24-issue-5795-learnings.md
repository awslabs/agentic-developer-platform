# Issue #5795 — Task API T2: authenticated task submission

Adding `POST /v1/tasks` to the existing main API Gateway and ingress Lambda.

## What generalises

### A guard that stops applying is worse than no guard

The #5653 postcondition requires every API Gateway route to map both provenance
headers. It read only `x-amazon-apigateway-any-method`, which was complete while
every route used an any-method key. My route is the first with an explicit
method, so the check would have **skipped** it — and reported success. The deploy
goes green, the invariant appears enforced, and an auth-NONE route forwards a
client-supplied identity header to a pod that treats it as proof of identity.

Widening it surfaced a second thing: the widened check then failed on the
placeholder body's `/status` MOCK route, which had been escaping only because it
uses an explicit `get`. That exemption was an accident that looked like a design.
Both are now explicit.

Generalisable: when adding the first instance of a new shape to something a guard
inspects, check whether the guard *sees* the new shape. "The check passed" and
"the check examined my change" are different claims, and the gap between them is
invisible in CI output.

### Match a permission's scope to the route, not to the neighbouring resource

The obvious move for the new `aws_lambda_permission` was to copy the broker's
`source_arn = "${execution_arn}/*/*"`. That would be materially worse here than
it is for the broker, because the target is the **ingress** Lambda, shared with
the HMAC-authenticated GitHub webhook route. A wildcard grant lets any route on
the API — including one added years later — invoke it, with the `resource` value
that route produced flowing into the ingress router's dispatch. Scoped it to
`/*/POST/v1/tasks`.

The lesson isn't "wildcards are bad"; it's that copying an adjacent resource's
scope copies an assumption about what that resource *is*. A single-purpose broker
Lambda and a multi-route ingress Lambda have different blast radii for the same
policy text.

### Two switches when the expensive one is slow to reverse

Route publication is a Terraform apply; admission is a Lambda env var. Collapsing
them into one flag would mean the only way to stop accepting tasks is an apply
that removes the route — slow, and it returns an API Gateway 403 rather than the
contract's refusal shape. Separating them lets the edge be published and verified
before anything is admitted, and lets admission be withdrawn in seconds.

### Name-based test selectors make evidence quietly weaker

I first registered the T2 manifest commands with `pytest -k 'replay or idempoten
or ...'`. A renamed or removed test silently selects fewer cases and still exits
0, so the criterion keeps reporting as covered. Replaced with unfiltered suite
paths, and added a checker rule that every `runnable` command names a path that
exists — the same failure one level up (a criterion marked runnable against a
test file that was renamed or never written reads as covered while running
nothing).

### Verify a new check by breaking the thing it checks

Every guard I added, I mutated the source to violate it and confirmed the
specific failure: widening the Lambda permission back to `/*/*`, neutering the
postcondition's method iteration, pointing a manifest command at a nonexistent
file, and flipping an unimplemented criterion to `runnable`. Three of the four
would have passed a naive reading of the code. This is cheap and it is the only
thing that distinguishes a check from a comment.

One hazard worth recording: I ran `git checkout <file>` to revert a mutation and
discarded my own uncommitted edits to that file along with it. Copy the good
version aside first (`cp file /tmp/good`) and restore from that; `git checkout`
reverts to the *index*, which knows nothing about work in progress.

## Repo-specific notes

- `sys.modules` surgery in a test fixture must **restore the same module
  objects**, not let them be re-imported. Deleting `task_api` without restoring
  gave me 14 failures that appeared only in the full run: later tests patched a
  freshly-imported second copy of the module while the code under test used the
  original. Individually every test passed. Save the dict, restore the dict.
- Local `ruff` (0.16.8) flags rules CI's pinned 0.15.20 does not. Measure a
  baseline by copying the module minus your own files to a temp dir and counting
  there, then compare — absolute counts are meaningless across versions.
  (64 → 62 here.)
- `modules/gateway/tests/` has a `conftest.py` importing sqlalchemy/fastapi, so
  the infra tests need `--noconftest` locally. `tests/test_route_prefix_convention.py`
  cannot collect without fastapi installed; pre-existing, unrelated.
- The `count` on a Lambda permission must key off a plan-time-known bool, never
  the computed invoke ARN. The broker resource documents this; the same applies
  to the new one.

## Cross-story finding, unresolved

The `POST /internal/v1/tasks/admit` route T2 forwards to **does not exist**. T3's
open PR #5945 implements `/internal/v1/agent/task-dispatch/*` — not the frozen
contract's `/internal/v1/tasks/dispatch/*` — and contains no admit route. T1
(#5944) is also unmerged, and V0 (#5821), the named start gate, is open with no
recorded evaluation outcome.

I implemented against the frozen contract rather than T3's prefix, and did not
invent a third variant. Consequence: T2-AC01's real end-to-end integration is
**unexercised** — evidence stops at the contract boundary with the admission call
mocked. Disclosed in the PR and on the issue rather than absorbed.

The useful generalisation: when a dependency you must call is absent, the choice
is between coding to the contract and coding to whatever a sibling PR happens to
have built. Coding to the contract keeps one authority; coding to the sibling
creates a second de-facto one that nobody agreed to. Either way the gap in
evidence has to be stated, because "tests pass" would otherwise imply an
integration that was never run.
