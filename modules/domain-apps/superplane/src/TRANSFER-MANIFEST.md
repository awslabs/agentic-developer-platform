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
mismatches=0`). The maintained tree now has intentional divergences, recorded below —
test-only in the controller, and behavioral in `superplane-api` from U14 (#5055) onward. File modes were preserved by transferring through `git archive | tar`
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
| API | `cd src/superplane-api && pip install -e ../../auth && pip install -e ".[dev]" -c ../../releases/transfer-constraints.txt && python3 -m pytest tests/` | **366 pass** (288 at adoption; U14 #5055 added the domain-auth suite). The policy package install is required from U14 onward, and so is the constraints file — see [Inherited findings](#inherited-findings) |
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

#### Observation receiver — issue #5056 (U15)

U15 implements the receiving end of the U8 observation contract in the maintained API, so
these files diverge from the adopted revision by design:

| Path | Change |
|------|--------|
| `superplane-api/app/config.py` | Adds `observation_submitters` (fail-closed, empty default) and `legacy_heartbeat_enabled`. |
| `superplane-api/app/models/observation.py` | New: `observation_receipts` (replay/idempotency state) and `observation_leases` (fence tokens). |
| `superplane-api/app/models/__init__.py` | Registers the two new models so Alembic discovers them. |
| `superplane-api/app/services/observations.py` | New: submitter resolution, storage-based ownership resolution, replay enforcement, persistence. |
| `superplane-api/app/services/leases.py` | New: lease acquire/release with receiver-assigned fence tokens. |
| `superplane-api/app/routers/heartbeat.py` | Adds the authenticated `/internal/observations*` routes; gates the legacy shared-token heartbeat on `legacy_heartbeat_enabled`. |
| `superplane-api/alembic/versions/011_add_observation_receiver_tables.py` | New additive revision for the two tables. Not applied; deployment is U23 (#5327). |
| `superplane-api/tests/test_models.py` | `test_all_tables_registered` asserts an *exact* table set, so the two new tables had to be added to it. |
| `superplane-api/tests/test_heartbeat_auth.py` | New: 59 cases covering R11 acceptances 2–4, replay/idempotency and lease behaviour. |

The `test_models.py` edit is the only change to a pre-existing *test* in this group. It was
not optional: the assertion is an equality against a literal set, so any new model fails it.

Two properties of the receiver are worth recording because a later "simplification" would
silently reintroduce the vulnerabilities they close. Ownership is resolved only through
`workspaces.cluster_id`: `shared_cluster_id` is many-to-one and would let any tenant on a
shared cluster write every other tenant's fleet state, and `clusters.workspace_id` is never
written by any application code in this tree, so trusting it would fail open. And released
leases keep their row so fence tokens stay monotonic; a `DELETE`-on-release, which is what
the old `reconcile_locks` path did, resets the sequence and defeats fencing.

`releases/superplane.lock.yaml` records `schema.observed.version_files: 17` after integrating
U13's migration-chain repair and U14's workspace-grant revision. The U15 revision is therefore
`011_add_observation_receiver_tables`, extends `010_add_workspace_grants`, and remains the
single head. `status: unverified` still records that deployment verification belongs to U23;
it does not describe the now-linear source tree as multi-headed.

##### Honest probe reporting in the monitor — R11 acceptance 4

R11 acceptance 4 requires that "a probe never reports health it did not check", and names
three defects in the transferred monitor. All three are repaired here, which means changing
transferred **non-test** code and the transferred tests that asserted the old behaviour:

| Path | Change |
|------|--------|
| `superplane-platform-monitor/monitors/monitor.go` | Adds `HealthStatusNotChecked`; corrects `HealthDimensionSeverity` so `Unreachable`(4) outranks `Unknown`(3) and `NotChecked`(1) sits between `Healthy` and `Degraded`; replaces `NoopEKSProber` with `UnconfiguredEKSProber` returning the new `ErrProbeNotPerformed`. |
| `superplane-platform-monitor/monitors/cluster_health.go` | Five dimension checks report `NotChecked` instead of `Healthy` when they had nothing to inspect (skypilot not reported, vault status empty, no node summary, node total 0, no cost data / no average / no history). `checkVaultSyncStatus`'s `default` now reports `Unknown` instead of `Healthy`. `checkEKSReachability` maps `ErrProbeNotPerformed` to `NotChecked` rather than to `Unreachable`. |
| `superplane-platform-monitor/main.go` | Wires `UnconfiguredEKSProber` in place of `NoopEKSProber`. |
| `superplane-platform-monitor/monitors/monitor_test.go` | `TestNoopEKSProber` asserted `ProbeEKS` returns `nil` — i.e. it asserted the defect. Replaced by `TestUnconfiguredEKSProber_ReportsProbeNotPerformed`. `TestWorstStatus_Table`'s "unknown is worst" case renamed and three ordering cases added. |
| `superplane-platform-monitor/monitors/cluster_health_test.go` | Five "expected Healthy when not reported" assertions inverted to `NotChecked`; `TestCheckAllDimensions_AllHealthy`, `TestCheck_FullCycle` and `TestCheck_NoHealthTransitionEvent` now supply all six dimensions' inputs (they previously expected an overall `Healthy` from three); `TestWorstStatus`'s table gains the corrected ordering cases. New cases: unrecognised vault status, zero-node summary, unconfigured prober, partial-report aggregate. |
| `superplane-platform-monitor/tests/integration_test.go` | `NoopEKSProber` → `UnconfiguredEKSProber` at 7 call sites (build-tagged `integration`, not run in the CI lane). |

The Go severity map is now value-for-value identical to `SEVERITY_RANK` in
`contracts/superplane_contracts/health.py`, so the two ends of the contract cannot rank the
same statuses differently.

One live producer/consumer mismatch is **recorded, not repaired**: the controller emits
`vault_sync_status: "synced"`, which the API's `^(ok|failed|pending)$` schema does not
allow. Under the transferred code that unrecognised value fell into a `default` branch that
reported `Healthy`, so the mismatch was invisible. It now reports `Unknown`, which is what
"I was told something I cannot interpret" means. Aligning the producer with the schema is
not this story's; the runbook
(`docs/runbooks/superplane-monitor-grant-withdrawal.md`) names it for the operator.

#### U14 (#5055) — domain token policy and workspace authorization enforcement

The first **behavioral** divergence in `superplane-api`, as distinct from the test-only
ones above. Recorded here because the sentence below requires it, and because a reviewer
diffing the maintained tree against the pinned reference will now get a non-empty result
for these paths and should find the reason stated rather than have to infer it.

New files (no upstream counterpart): `app/auth.py`, `app/domain_guard.py`,
`app/endpoint_inventory.py`, `app/models/workspace_grant.py`,
`alembic/versions/010_add_workspace_grants.py`, `scripts/stage-domain-auth.sh`.

Modified: `app/main.py` (registers the guard), `app/config.py` (enforcement settings,
including `cognito_enabled` recorded rather than inferred), `app/middleware/auth.py`
(reads the guard's verified caller instead of re-deriving identity; the legacy HS256
decoder is consulted only when no verified caller exists — it is not a fallback for a
rejected one), `app/models/__init__.py`, `app/routers/{internal,heartbeat,cost}.py`
(the two `/internal/*` routes that had no authentication, plus a missing-header 401 that
was previously a 422), `app/routers/research.py` and `app/services/scanner.py` (the
recorded actor comes from the verified caller, and every research read, aggregate and
write is scoped through the row's server-held workspace tenant; null/dangling legacy
rows fail closed), `tests/{test_auth.py,conftest.py,test_models.py,test_proxy.py}`.

Outside `src/`: `.github/workflows/superplane-domain-ci.yml` now runs
`pip install -e ../../auth` before the API's own install. `app/auth.py` imports the
policy package, and that package is not on any index, so the transferred suite no longer
installs from its `pyproject.toml` alone. Recorded here because it is the one place the
transferred tree's dependencies stopped being self-contained.

Enforcement is off by default (`domain_auth_enforced`), so this changes no deployed
behavior until an operator turns it on with an issuer, a JWKS URL and a client
allowlist — a missing allowlist fails startup rather than defaulting to "any client".
Retiring the legacy identity path is U21's separate conditional story and is NOT done
here.

#### U16b (#5057) — signed EKS auth and mandatory TLS verification

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

`src/superplane-api/alembic/` (17 files under `versions/`, with `alembic.ini` — 13 at the
transfer, two added by U13, U14's `010_add_workspace_grants.py`, and U15's
`011_add_observation_receiver_tables.py`) is the **only** migration directory
this transfer brings, and it belongs to the API's own database. It does not touch the gateway's migrations or any shared schema, and nothing
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

### 2. Alembic migration chain — repaired by U13 (#5045)

The transferred 13-file chain reused revision ids `006` and `007` and referenced a
missing `005`, so Alembic could not even load its revision map. U13 preserved applied
revisions 001-005, relinked only the unreachable revisions, and added the missing
`api_keys` and `budget_alerts` tables. Offline checks now prove one base, one head,
no duplicate or dangling ids, and model/table parity. The lock remains
`schema.status: unverified` because a real-database upgrade/restore rehearsal is still
deferred; that is a live acceptance gate, not an unresolved source graph.

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

### 3. Clock-dependent cost-reconciler tests — repaired with U13 (#5045)

Two tests in `src/superplane-api/tests/test_cost_reconciler.py` read the real clock and
seeded node intervals relative to UTC midnight, while `_compute_daily_cost` clamps a
node's end to `now`. Between 00:00 and 03:00 UTC the seeded interval was still in the
future, the node was skipped, and the expected cost arrived as `0`.

**Not a flake in the usual sense — deterministic per hour.** Sweeping a pinned clock
across the day: 2 failures at 00:30 and 01:30, 1 at 02:30, 0 from 03:30 onward. So this
blocked every PR in a 3-hour daily window and was invisible the other 21 hours, which is
why it survived the transfer and the units after it. Observed here as a real CI failure
at 00:42 UTC.

Repaired rather than recorded, because unlike the findings around it this one has no
owner queued and it gates an unrelated PR's required check. Both tests already pass
`day_start` and `now` as arguments to the method under test, so deriving `now` from a
fixed `day_start` expresses the intended scenario more directly than reading the clock
did — no production code changed, and the assertions are unweakened. This is the second
behavioral divergence in `superplane-api`; byte-fidelity with the origin is already
broken for this component by the entry above, and the alternative was leaving a
guaranteed daily red window in a lane three other units declare as required.

### 4. Insecure defaults and origin-specific values — U14 / U3

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
