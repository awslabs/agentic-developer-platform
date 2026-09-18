# Superplane source transfer manifest

**Issue #5326 (U22), EPIC #4910, wave 5.** This directory holds the three Superplane
components ADP now **maintains**. It is the authoritative, writable source for them.

Before this transfer, ADP pinned a revision of `aws-innovate/AISuperPlane` that the
hosted agent token cannot read, and the three image-build lanes stopped with a named
"source access unresolved" error rather than building anything. The design allocation
for this EPIC (`repo-path-allocation.md` §3) required that *if* source were
transferred, its tests, CI and ownership move with it and no writable duplicate be
maintained. That is what this manifest records.

Two things this transfer explicitly does **not** claim: that these components are
deployable, and that their inherited defects are repaired. See
[Inherited findings](#inherited-findings) — U13 owns migration repair, U14 auth
enforcement, U15 the monitor boundary, and U23 the deployable full release.

---

## Old path → maintained path

The origin layout is preserved exactly, so a reviewer can diff this tree against the
origin revision and get an empty result. Renaming directories on the way in would have
made every future comparison a manual exercise.

| Origin path (`aws-innovate/AISuperPlane` @ `5d543c95`) | Maintained path | Files |
|---|---|---|
| `src/superplane-api/` | `modules/domain-apps/superplane/src/superplane-api/` | 107 |
| `src/superplane-controller/` | `modules/domain-apps/superplane/src/superplane-controller/` | 39 |
| `src/superplane-platform-monitor/` | `modules/domain-apps/superplane/src/superplane-platform-monitor/` | 15 |

**161 files total.** At adoption, every file was byte-identical to the origin revision,
verified by comparing Git blob IDs rather than by eye or by diff summary (`checked=161
mismatches=0`). The maintained tree now has intentional, test-only divergences,
recorded below. File modes were preserved by transferring through `git archive | tar`
rather than `cp`: `superplane-controller/tests/e2e-controller-test.sh` is mode `100755`
in both trees, and a lost executable bit on a test entrypoint is the kind of difference
that surfaces only when someone tries to run it.

### Provenance is recorded in two places, deliberately separated

| Fact | Where it lives | What it means |
|---|---|---|
| ADP commit | git history of this repository | What is maintained now; changes with every fix |
| ADP commit adopted from | `releases/superplane.lock.yaml` → `maintained_source.adopted_from_adp_commit` | The pinned ADP reference this story worked from (`8f01c1ed`) |
| Origin repository + revision | `releases/superplane.lock.yaml` → `upstream` (marked `role: … not a build input`) | Historical origin; an image label, never a fetch target |

A single "revision" field could only have carried one of these. Whichever it carried, a
reader would lose the ability to say whether the maintained files have changed since the
transfer — which is the one question this manifest exists to answer.

### Licensing

The origin repository contains **no `LICENSE` and no `NOTICE` file** at the transferred
revision, and none has been added here. Fabricating a license for someone else's code
would be a false statement about its terms, which is worse than the gap it papers over.
Existing per-file headers and attribution transferred unchanged with the files.

---

## What was included, and what was not

Upstream `src/` holds seven components. Three were retained; the accepted topology for
this EPIC does not include the other four, so they are not transferred and no writable
copy of them exists in ADP.

| Component | Status | Reason |
|---|---|---|
| `superplane-api` | **transferred** | FastAPI control-plane API; retained in the accepted topology |
| `superplane-controller` | **transferred** | Go controller; retained |
| `superplane-platform-monitor` | **transferred** | Go platform monitor; retained |
| `superplane-agent-gateway` | excluded | Not in the accepted topology; ADP's own gateway (`modules/gateway/`) is the platform's tenant-facing proxy, so transferring this would create a second, competing one |
| `superplane-cli` | excluded | Not in the accepted topology |
| `superplane-portal` | excluded | Not in the accepted topology; ADP's gateway frontend is the operator surface |
| `superplane-skill` | excluded | Not in the accepted topology |

Also **not** transferred:

- **Upstream platform infrastructure** (`infra/control-plane*`, `infra/account-factory`,
  `infra/arc-runner`, `infra/observability`, `infra/graphiti*`, and the rest). ADP has
  its own platform under `platform/infra/`, and importing a second one would fork the
  VPC/EKS/IAM boundaries this repo already owns. Deployment assets for the retained
  components are U3's, reconciled into existing ADP infrastructure rather than adopted
  wholesale.
- **`.github-agent/setup/setup-config.env`.** Excluded by the story, not read, and not
  restored to any value. It was the sole file differing between the ADP reference
  snapshot and live upstream.
- **The reference snapshot** at `modules/domain-apps/ai-super-plane/reference/`. It
  stays read-only evidence at the origin revision. It is not a build input, not a test
  input, and must not become a second writable runtime tree alongside this one. The
  build script refuses a context under it, and
  `tests/test_build_source_is_declared.py` asserts no lane reads it.

---

## Supported commands

Everything below runs from a **clean ADP checkout** with no upstream access, no PAT and
no reference tree. That property is the point of the transfer, and it is asserted by
tests rather than described: `test_build_reads_lock.py` resolves each component's build
context and checks the directory and its Dockerfile exist in the checkout.

### Tests

| Component | Command | Notes |
|---|---|---|
| API | `cd src/superplane-api && pip install -e ".[dev]" -c ../../releases/transfer-constraints.txt && python3 -m pytest tests/` | **288 pass.** The constraints file is required — see [Inherited findings](#inherited-findings) |
| Controller | `cd src/superplane-controller && go vet ./... && go test ./... -count=1` | Go 1.23 (the version its `go.mod` declares) |
| Platform monitor | `cd src/superplane-platform-monitor && go vet ./... && go test ./... -count=1` | Go 1.23 |
| Rest of the module | `python3 -m pytest modules/domain-apps/superplane/ -m "not superplane_live"` | Excludes `src/` by `conftest.py`'s `collect_ignore` — see [Boundaries](#boundaries) |

Not run in offline CI, and not because they were suppressed:

- `src/superplane-controller/tests/e2e-controller-test.sh` drives a real cluster. It
  transferred with its executable bit intact and belongs to credentialed live
  acceptance, not to a lint-plus-unit-test lane. `go test ./...` does not pick it up.
- `src/superplane-platform-monitor/tests/integration_test.go` carries
  `//go:build integration` and requires a real PostgreSQL via
  `INTEGRATION_DATABASE_URL`. The default build excludes it by the file's own design;
  the lane does not pass `-tags=integration`.

### Builds

| Component | Command |
|---|---|
| Any of the three | `python3 releases/resolve_lock.py <component>` → emits `SUPERPLANE_SOURCE_DIR`; then `releases/build-image.sh <component>` |

The build context comes from the lock, not from the lane, so moving a component is a
lock edit rather than a hunt through three workflows. Image tags are the **ADP commit**;
the origin revision rides along as an image label. That inverted with the transfer: the
origin revision is frozen at the transfer point, so tagging by it would make every later
fix overwrite the previous image under an unchanged tag.

### CI

`.github/workflows/superplane-domain-ci.yml` (offline, no AWS identity) runs the API and
Go suites above on every PR touching `modules/domain-apps/superplane/**`. The three
build lanes (`superplane-{api,controller,monitor}-build.yml`) watch
`src/superplane-*/**` in addition to the lock, so a change to maintained code can
actually cause a rebuild — watching only the lock would let an owned component drift
from the image running in a cluster.

---

## Boundaries

Three narrow exclusions exist, each scoped to one mechanism and each with its own
reason. None of them exempts `src/` from CI, from path filters or from review.

| Exclusion | Where | Scope | Why |
|---|---|---|---|
| ADP lint rules | `../.ruff.toml` → `extend-exclude = ["src"]` | **lint only** | Under ADP's ruleset (the gateway's `E,F,I,N,W,UP`) `ruff format` rewrites **66 of 98** files and `ruff check` reports **116** findings — mostly import ordering and `datetime.utcnow`. Reformatting destroys the byte-fidelity that makes this transfer auditable, and the findings are the origin's existing style, not defects introduced here. Under ruff's *default* rules the same tree is clean, so this exclusion's size is a function of which rules ADP selects |
| Module-wide pytest collection | `../conftest.py` → `collect_ignore = ["src"]` | **collection only** | These tests import their own dependency set. Without this, one uninstalled dependency aborts *collection* and reports zero results for the other ~1790 tests in the module |
| Go integration/e2e suites | the files' own build tags | as designed upstream | Need a real database / cluster; see the table above |

Removing the lint exclusion is a separate decision for whoever adopts ADP conventions
into this tree wholesale. Doing it in the same commit as a behavioral fix would bury
that fix in a reformat.

### Maintained divergences from the adopted revision

`src/superplane-controller/controllers/cost_reconciler_test.go` renames its package-level
test helpers to `costFakeClock`, `makeCostNodePool`, and `makeCostNode`. The adopted
revision declared `fakeClock`, `makeNodePool`, and `makeSuperplaneNode` again in other
files in the same `controllers` package, with incompatible definitions. As a result,
`go vet ./...` and `go test ./... -count=1` failed at package compilation before 82
controller tests could run. The names are test-only; production behavior and assertions
are unchanged. This explicit divergence makes the supported controller command runnable
without weakening the CI gate.

The same test file compares aggregated currency values with a `1e-9` tolerance rather
than exact floating-point equality. Runtime accumulation differs by one ULP from the
constant-folded expectation, causing the otherwise-correct test to fail deterministically.
The tolerance remains far below a meaningful currency change and leaves production code
unchanged.

`src/superplane-api/app/services/` gains `eks_auth.py`, and `proxy.py`, `kubeconfig.py`
and `routers/workspaces.py` diverge from the adopted revision, for the R12 cluster-
authentication repair (U16b, #5057). The adopted revision sent the STS `SessionToken`
returned by `AssumeRole` as the Kubernetes bearer token and disabled TLS verification
whenever cluster CA data was absent — with `ca_data` hardcoded to `""` on the proxy path,
so the unverified branch was the one that always ran. Both are corrected here rather than
recorded as inherited findings, because they are the defect the story owns: cluster calls
now use a signed, cluster-bound `k8s-aws-v1.` token, kubeconfig export carries an
`aws eks get-token` exec block instead of an inlined credential, and no code path can
disable verification. This is a production-behavior divergence, unlike the test-only ones
above.

Two further divergences in the same files came out of review of that change. First, an
exported kubeconfig for a tenant whose role requires an `sts:ExternalId` now delegates role
assumption to a locally configured AWS profile instead of setting an `AWS_EXTERNAL_ID`
environment variable: the AWS CLI honours no such variable and `aws eks get-token` has no
`--external-id` flag, so the variable was inert and the export would have failed with
AccessDenied against exactly the trust policies it appeared to satisfy. `--role-arn` is
omitted on that path because supplying it alongside a profile makes the CLI mint the token
with an AssumeRole call that carries no `ExternalId`. Second, `write_ca_bundle` now derives
its path from a hash of the CA content and reuses it, rather than calling `mkstemp` per
invocation; the bundle is written on every brokered request and cannot be deleted on return
(the Kubernetes client re-reads `ssl_ca_cert` per request), so the original form grew `/tmp`
without bound once the verified path became reachable.

Superplane domain maintainers own this manually maintained inventory. Any further change
from the adopted revision must be recorded here; the historical reference is deliberately
not made available to CI as a build or comparison input.

### Migration ownership is unchanged

`src/superplane-api/alembic/` (13 files under `versions/`, with `alembic.ini`) is the
**only** migration directory this transfer brings, and it belongs to the API's own
database. It does not touch the gateway's migrations or any shared schema, and nothing
in the transferred source or the build lanes can reach them. The guard on this predates
U22 and still holds.

---

## Inherited findings

Recorded honestly rather than repaired here or marked healthy. Each names the unit that
owns it. **None of these was introduced by the transfer**; they are properties of the
code at the origin revision, which is why they survived a byte-identical copy.

### 1. Unbounded FastAPI floor makes the suite non-reproducible — U23

`src/superplane-api/pyproject.toml` declares `fastapi>=0.115.0` with no upper bound, so
what gets installed depends on when the install runs. Today that resolves to FastAPI
0.141.1, and one test fails there.

**What breaks:** FastAPI 0.137.0 changed the contents of `app.routes`. An included
router now leaves an `_IncludedRouter` entry in that list instead of flattened `Route`
objects, so code walking `app.routes` for `.path` sees only the 4 built-in docs routes.
`tests/test_users.py::TestListUsersEndpoint::test_list_does_not_require_admin_role` does
exactly that and asserts `"/users" in routes`. Bisected: 0.136.0 → all 288 pass;
0.137.0 → this one test fails and nothing else changes.

**What does NOT break — checked, not assumed:** the routes still serve.
`app.openapi()["paths"]` contains 43 paths including `/users` and `/accounts`; `GET
/users` and `GET /accounts` return **401** (route present, auth required) and
`GET /health` returns 200. A dropped route returns 404. So this is a test coupled to a
changed introspection API, not an app that stopped serving.

> An earlier draft of this manifest claimed `include_router()` "registers nothing" and
> that an image built at the floor would 404 every endpoint. That was wrong. It is
> recorded here rather than quietly deleted because the error had a direction: it would
> have sent U23 hunting a routing outage that does not exist, and it overstated what is
> at risk in shipping the image. Verifying the claim is what surfaced it.

- **Pinned for CI** by `releases/transfer-constraints.txt` (`fastapi==0.115.6`,
  `starlette==0.41.3`); the suite is then 288 passed. The transferred `pyproject.toml`
  is deliberately *not* edited, because byte-fidelity is what makes this tree auditable.
  The durable fixes are to tighten the floor or to assert against `app.openapi()`, which
  is stable across the change.
- **NOT applied to the image.** `src/superplane-api/Dockerfile` runs
  `pip install --prefix=/install .` from the same unbounded pyproject and the constraints
  file does not reach the Docker build, so an image resolves whatever FastAPI is current.
  On the evidence above that yields a working app — but two builds of the same commit can
  still ship different dependency trees, which is a reproducibility defect on its own.
  **U23 should pin the floor for that reason, not because routing is broken.**

### 2. Broken Alembic migration chain — U13 (#5045)

Of the 13 files in `src/superplane-api/alembic/versions/`, revision id `006` is declared
by three separate files and `007` by three more, several sharing the same
`down_revision`. `alembic upgrade head` cannot resolve that. The lock records this as
`schema.status: unverified` with `single_head: false` and the observed duplicate counts,
rather than asserting a compatible version that was never verified. **U13 owns the
repair; it remains explicitly unresolved and is not marked healthy here.**

**One count was corrected by this transfer.** The lock previously recorded `"006": 4`.
Counting the maintained chain gives three: there *are* four `006_*.py` filenames, but
`006_account_onboard_fields.py` and `006_add_default_workspace_fields.py` declare their
full descriptive ids, so only three collide on the bare `"006"`. The filename prefix and
the declared `revision =` value are different things, and only the second one collides.

This is not cosmetic — `infra/scripts/check_migration_contract.py` renders that count
into the refusal an operator reads to learn what U13 has to fix, so an inflated number
sends them looking for a fourth conflicting file that does not exist. U22 is the first
unit that could check it: before the transfer, nobody in ADP had the files to count. A
test now derives the counts from the chain itself, so the lock and the files cannot drift
apart again. The conclusion is unchanged — still multiple heads, still unverified.

### 3. Insecure defaults and origin-specific values — U14 / U3

Present in the transferred code as-is, and left as-is because rewriting them would be
implementing another unit's story inside a transfer commit:

- `jwt_secret_key: str = "CHANGE-ME-IN-PRODUCTION"` in `superplane-api/app/config.py`.
  **U14** owns auth enforcement.
- Upstream's AWS account ids in **six** transferred files across **two** components:

  | Account | Transferred carrier |
  |---|---|
  | `605440105851` | `superplane-api/deploy/config.env` |
  | `605440105851` | `superplane-api/deploy/db-migrate-job.yaml` |
  | `605440105851` | `superplane-api/deploy/deployment.yaml` |
  | `605440105851` | `superplane-api/deploy/integration-test.yaml` |
  | `605440105851` | `superplane-controller/deploy/controller.yaml` |
  | `938500344975` | `superplane-api/deploy/db-seed-job.yaml` |

  Earlier versions first attributed the values to the API alone, then enumerated only the
  first account id; those omissions are precisely what makes an inventory unsafe to rely on.
  The completeness test derives carriers for both ids and requires every account/path pair.
- `:latest` image tags in the same two components' manifests plus
  `superplane-platform-monitor/deploy/deployment.yaml` — so all three ship at least one
  floating tag.
- An inline development database password in `superplane-api/deploy/integration-test.yaml`
  (a local Postgres for the integration suite, not a deployed credential).

These values are **not** adopted as ADP configuration anywhere. The lock deliberately
records no account id — `tests/test_lock.py` fails on any 12-digit number in a value
position, precisely so upstream's account cannot become ADP's by being copied — and
`skypilot_config.deployment_target` stays `unresolved`. The deploy assets are inventoried
as transferred runtime files, not applied; U3 owns reconciling them into ADP
infrastructure, where the account and credentials come from ADP's own configuration.

---

## Maintainer ownership

| Surface | Owner |
|---|---|
| Transferred source under `src/` | ADP platform team (this repository), via the units below |
| Release lock, resolver, build lanes, buildspecs | U2 (#5041) |
| Terraform, rollout, deployment asset reconciliation | U3 |
| Migration chain repair | U13 (#5045) |
| Auth enforcement | U14 |
| Platform monitor boundary | U15 |
| Deployable full release | U23 |
| This manifest and the transfer itself | U22 (#5326) |

Changes to this tree are ordinary ADP changes: a PR, the domain CI lane, review. There
is no upstream to send them to, and no second writable copy to keep in sync.
