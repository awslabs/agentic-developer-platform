# Superplane live acceptance

Wave evaluations #5067–#5070 remain open until their real-boundary criteria pass.
Offline tests, fixture matches, missing inputs and skipped tests do not establish
live acceptance. Implementation stories own these checks; operations executes them.

## U1: authenticated feature API observation

`test_u1_features_live.py` implements the three **API** assertions under #5067's
"Live frontend/API contract": the 12 named fields are boolean, `superplane` is
false, and every field in the reviewed backend fixture is present. This is the
API-check portion of #5288. Its fixture/provenance were merged in #5290. Values
of other live flags may differ from the fixture defaults; additional fields are
allowed. The fixture's byte hash is pinned so a locally weakened fixture cannot
silently narrow the check. Review provenance and update the pin when the fixture
is deliberately changed.

Use the existing authorized `embark1/dev` target and an existing short-lived ADP
token with feature-API access. Supply the token through `SUPERPLANE_LIVE_ADP_TOKEN`
in the invoking process environment; do not put it in source, command arguments,
shell history, fixtures, reports or GitHub. This check neither acquires credentials
nor grants access. With that variable already set, run from the repository root:

```sh
export SUPERPLANE_LIVE_ENVIRONMENT='embark1/dev'
export SUPERPLANE_LIVE_FEATURES_EVIDENCE_FILE='/absolute/new/path/u1-features.json'
python3 -m pytest modules/domain-apps/superplane/tests/acceptance/test_u1_features_live.py -q --tb=short
```

The checker performs one authenticated HTTPS GET to the reviewed origin's
`/api/features`. The environment mapping is the same reviewed registry used by U6
below. No arbitrary URL override, redirect, environment proxy, feature change,
deployment or provider operation is supported. A unique query nonce and cache
control request fresh data; nonzero response Age, a missing/invalid Date, or a Date
more than two minutes from the local request time refuses the observation. Keep the
operator clock accurate. HTTP denial/error, invalid JSON/duplicate fields,
oversized data, absent/invalid inputs and missing/modified fixtures fail visibly.
An explicit invocation does not skip. No token or raw response/error body is
recorded. Failure-output regressions invoke the actual live test with offline
HTTP responses and `--tb=long --showlocals`, checking schema/JSON/callback,
redirect and post-token transport failures for private data in the complete
diagnostic stream. Raw-data frames are hidden and callback exception chains
are detached before the sanitized error reaches pytest.
The new evidence file is published atomically with private permissions
after all assertions succeed; existing files and symlinks are never overwritten.

The record is labelled **U1 feature API only**, `live` / `observed`, with
`u1_acceptance: incomplete`. It contains observation times, selected endpoint and
registry target metadata, fixture/response hashes and the three assertion results.
It does not establish an unset deployment environment, a deployed source revision,
AWS/cluster identity, browser gating, enabled behavior or deploy/undeploy cleanup.
Registry account metadata identifies the selected target; this check makes no STS
or Kubernetes identity observation. Injected transports produce `offline-fixture`
records and cannot be published by the live entry point.

This command is an equivalent implementation mapping for the API subsection only.
It does not replace the proposed full `test_u1_live.py` or U1-L1's teardown check.
That observer must consume U3's final reviewed inventory/lifecycle contracts and
authorized deploy/undeploy evidence. #5288, #5067 and U1 acceptance remain open.

## U6: CLI-only delivery

`test_u6_live.py` implements the delivery half of R16 acceptance 1 for #5039,
tracked by follow-up #5285. It performs only GitHub GET requests and public HTTPS
CLI downloads. It neither deploys nor executes downloaded scripts, and needs no
AWS credential or ADP token. GitHub reads use the existing authenticated `gh` CLI.

Select an actual reviewed, merged PR that changes `adp-superplane.py`, with all
changes under `modules/gateway/cli/`, and its successful `gateway-deploy.yml`
**push** run. The backend deployment job must have run successfully. The extension
must differ from its first-parent content so an old deployment cannot appear new.
The helper, `adp`, and `install.sh` must all match that exact merge when downloaded.
A run/head change during the check invalidates the result. API pagination is
checked; an incomplete diff never establishes a CLI-only change.

The reviewed target registry currently contains only `embark1/dev`:
`https://d1g6cal2ts4iis.cloudfront.net`, account `879318057152`, `us-east-1`.
These are existing release-target identifiers, not permission to deploy anything.
Adding another target requires its own reviewed mapping and authorization.

Run from the ADP repository root with explicit inputs:

```sh
export SUPERPLANE_LIVE_ENVIRONMENT='embark1/dev'
export SUPERPLANE_LIVE_CLI_MERGE_SHA='<qualifying 40-character merged commit>'
export SUPERPLANE_LIVE_CLI_RUN_ID='<successful push deployment run ID>'
export SUPERPLANE_LIVE_EVIDENCE_FILE='/absolute/new/path/u6-delivery-evidence.json'
python3 -m pytest modules/domain-apps/superplane/tests/acceptance/test_u6_live.py -q
```

Use an existing output directory and a new evidence filename. Missing inputs fail
with `BLOCKED`; the test does not skip. Unavailable GitHub/HTTP access, a mixed
CLI/backend change, a manual dispatch, an absent/skipped backend job or mismatched
served bytes fail the check. No passing record is written on failure. On success,
the record contains timestamp, selected target, commit/parent/PR, run/attempt/job,
changed paths and HTTP/hash observations. It contains no credentials, downloaded
source bodies or GitHub patch text. Redirected artifact responses are rejected.

The U6 implementation merge `024787f28c92bc533d6ef87202629abda8c53d06` is **not** a
qualifying event: it also changed gateway source. Its files are currently served,
but that narrower observation does not prove CLI-only triggering. Wait for a
qualifying authorized change; do not manufacture a commit/deployment to pass this
test. This follow-up's merge also does not itself satisfy the live criterion.

## Offline CI

Routine checks use:

```sh
python3 -m pytest modules/domain-apps/superplane/ -m 'not superplane_live' -q
```

Only explicitly marked real-environment tests are deselected. Offline regression
cases exercise wrong/stale run evidence, skipped deployment, hidden diff pages,
foreign renames, unchanged extension bytes, wrong served files and changing run
attempts. Their injected transports produce `offline-fixture`/`matched` results,
not `live`/`passed` evidence. A subprocess regression verifies that the explicit
live pytest command fails before networking when its inputs are absent.

The U1 teardown and U12 real baseline acceptance checks remain separate
prerequisites tracked by #5067. The API observation and U6 check do not close them.
