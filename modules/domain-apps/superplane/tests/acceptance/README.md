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
It does not replace `test_u1_live.py` below or U1-L1's teardown check. #5067 and
U1 acceptance remain open.

## U1-L1: post-undeploy resource absence

`test_u1_live.py` implements **R1 acceptance 4 / U1-L1**: after an independently
authorized deploy/undeploy sequence, the resources that deployment actually created
are absent from the real account and cluster. It is the teardown half of U1; the
feature-API check above is the other half, and neither closes the other.

### Why this check is shaped the way it is

"The resources are gone" and "I could not see any resources" look identical in a
report, and only the first is acceptance. A supplied empty list, a mock inventory,
an unexecuted plan, an IAM denial and a malformed response would each produce a
clean-looking "nothing found". So none of them can pass here:

- **The inventory is derived from the source that was actually deployed.** The
  expected set is computed from U3's merged sources — `infra/control-plane/{main,
  config,irsa}.tf` via `control-plane/tests/source_derived_names.py`,
  `releases/superplane.lock.yaml`, the `kind: Namespace` objects in `k8s/*.yaml`, and
  the environment's own `superplane.tfvars` (the file the apply and destroy lanes pass
  with `-var-file`). Those files are fetched **at the deploy run's own revision**, not
  read from the verifier's checkout: otherwise a resource the deploy created and a
  later commit renamed or removed would silently stop being checked, and a teardown
  that left it behind would still pass. Every file read is hashed into the evidence,
  so a reader can confirm which contract text produced the inventory. Your receipt
  must **cover** that derived set; an omission is an incomplete-coverage refusal.
  Extra domain-owned entries are allowed and are also checked.
- **The pre-teardown input is an observation receipt, not a list of names.** A list
  of names establishes nothing about whether those resources ever existed — it can be
  typed up after the teardown by copying the names the verifier itself derives, and
  resources that were never created would then be reported as cleaned up. So the
  input must be a `superplane.u1l1.pre-teardown-observation/1` receipt produced by
  `record_pre_teardown` (below) while the deployment still existed, in which every
  entry carries `observation: present`. It is cross-bound to the verified deploy run
  and attempt, the cluster ARN read just now, the derivation hashes, and a timestamp
  window between the deploy finishing and the teardown starting — a window that has
  already closed by the time the check runs.
- **A receipt is only evidence if somebody other than its author vouches for it.**
  Every field above is still a field in a file the caller holds, so on its own the
  receipt is a caller-authored document carrying authoritative-sounding labels. Two
  rules close that. First, a receipt whose own `evidence_kind` is not `live` — an
  `offline-fixture` record, which is what this module's injected transports produce —
  is refused on the live path outright, so a test double can never become acceptance
  input. Second, the receipt must be authenticated through a channel its author does
  not control: GitHub's record of the artifact a verified recorder run uploaded. The
  downloaded archive must hash to the digest GitHub computed, the JSON inside it must
  **equal** the receipt supplied, GitHub's own `created_at` must fall inside the
  closed window, the recording step must be proven to have executed on that attempt,
  and `recorder_sha256` is recomputed from the verifier module **at the recorder run's
  own revision** rather than trusted as written. Nothing publishes that artifact
  today — see "Known gap: no lane publishes execution evidence" below.
- **The same rule applies to the operations themselves.** A run ID and a green
  conclusion establish that a workflow ran, not *what it did to which resources*. So
  each lane must also publish a `superplane.u1l1.execution-attestation/1` document as
  a workflow artifact, and the authority is GitHub's artifact record — a unique name
  within the run, `expired: false`, the `workflow_run` run id and `head_sha`, GitHub's
  digest, GitHub's `created_at` — never anything the document says about itself. The
  downloaded `.zip` must hash to that digest, and then every field is cross-checked
  against a fact established independently: the account STS resolves to now, the
  selected environment and region, the Terraform state bucket and key derived from
  `environments/<env>/modules/superplane-backend.tfvars` at the deployed revision
  (with its deliberate `ACCOUNT_ID` placeholder resolved from the STS account), the
  run identity and revision already verified, the cluster and cluster ARN for
  Kubernetes lanes, and the derived resource identities carrying the plan action that
  role owes (`create` for deploy/rollout, `delete` for the teardown lanes). The
  attested publishing step must be one of the steps already proven to have executed.
  An attestation that omits a derived resource is an incomplete-coverage refusal;
  extra entries are allowed.
- **Which attempt produced an artifact is established from fields GitHub actually
  sends.** The artifacts API's `workflow_run` object carries exactly `id`,
  `repository_id`, `head_repository_id`, `head_branch` and `head_sha` — there is **no
  `run_attempt`** in it. A check that requires one refuses every real artifact while
  accepting a fabricated record that supplies it, which is the wrong way round. So the
  attempt is read from `actions/runs/{id}/attempts/{n}`, which must report that
  attempt's own number, the same run id, the revision under verification, a successful
  conclusion and a start/end window; an unreadable or inconsistent record blocks. The
  artifact is then placed on that attempt by three independent facts: GitHub's
  `workflow_run.id` ties it to the run, `workflow_run.head_sha` to the revision, and
  GitHub's `created_at` must fall inside the attempt's window. Because a run's attempts
  are consecutive in time, a leftover from an earlier attempt falls before this
  attempt's start and is refused — the re-run case, decided from stamped data. Only
  after that is the document's own `run_id` / `run_attempt` / `revision` compared
  against this independent evidence; the document never selects the attempt it is
  judged on. **Producers must therefore upload the attestation during the attempt that
  did the work**, from a step proven to have executed, rather than attaching it later.
- **Ownership is U3's decision, not a second rule.** Every identity is attributed
  with `infra/scripts/domain_ownership.py`, so a foreign name that merely contains
  the domain prefix, another module's instance of a type this domain owns, a
  repository the lock does not declare, or a core namespace cannot be reported
  absent. Attribution happens before any lookup is issued.
- **Absence is three-way.** Each resource is classified `absent`, `present` or
  `indeterminate`. A denial, throttle, expired credential, unreachable cluster,
  unreadable answer, or a not-found token belonging to a *different* API is
  indeterminate and **blocks**. It is never folded into absent.
- **The operations must be shown to have done the work, step by step.** A run-level
  `conclusion: success` is not enough. `superplane-infra-destroy.yml` sets
  `EMPTY_STATE=true` when Terraform's state is empty and then skips saving the plan,
  validating ownership and the destroy itself — while still concluding `success`. A
  run that tore nothing down is indistinguishable from one that tore everything down
  if you only read the conclusion. So each lane's load-bearing steps are fetched
  per-attempt (`actions/runs/{id}/attempts/{n}/jobs`) and each must have concluded
  `success`; a `skipped` step refuses. A renamed step also refuses, because the check
  can no longer prove what it claims.
- **The observation is bound to reality.** The credentials must resolve to the
  selected account; the named cluster must be in that account and region; and the
  active kubectl context's server must equal the EKS endpoint AWS reports for that
  cluster, so namespace reads cannot come from elsewhere. The deploy and undeploy
  must each be a deliberate `workflow_dispatch` of this module's own dispatch-only
  `superplane-infra-apply.yml` / `superplane-infra-destroy.yml`, in this repository,
  at the stated revision, on a recorded attempt — with the destroy **starting** after
  the apply finished. (Ordering is on the destroy's start: one that was already
  running while the apply was still creating resources would observe a half-built
  deployment.)

### The Kubernetes inventory is every rendered object, with U3's own lifecycle

The expected Kubernetes set is not "the namespaces". It is **every** object U3's
`k8s/*.yaml` render at the deployed revision — ten of them: the namespace, the
Deployment, the Service, three NetworkPolicies, the ConfigMap, the Role, the
RoleBinding and the ServiceAccount. Each is then classified by reading the
`kubectl delete` lines out of `k8s/rollback.sh` **at that same revision**, so U3's
teardown script stays the single statement of what its teardown removes:

- **9 DELETED** — the namespaced workloads and identities the script deletes in
  reverse dependency order. These are the objects held to an absence standard.
- **1 RETAINED** — the namespace. `rollback.sh` states in its own output that it
  deliberately does not delete it, because that would take the out-of-band
  `skypilot-api-db` Secret with it. U3 retains it, so this check does not require it
  to be gone, and **no namespace-deletion requirement is invented here**. It is
  checked against the contract that actually exists: if a RETAINED object is *absent*,
  that is reported as a deviation from U3's teardown contract — something deleted the
  namespace and took a Secret this module never created with it.

Together with the 16 AWS resources (2 IAM roles, 11 SSM parameters, 3 ECR
repositories) the derived inventory is **26 entries**, of which 17 are not in the
DELETED set.

### Known gap: no lane publishes execution evidence

The DELETED Kubernetes portion currently **BLOCKS**, and that is the accurate outcome
rather than a missing feature. `superplane-infra-destroy.yml` states in its own
summary that Kubernetes objects survive it and defers to "the rollout lane's own
teardown". `superplane-k8s-deploy.yml` has no teardown — its only mutating step is
`Apply`. `k8s/rollback.sh --teardown` *does* delete exactly those nine objects, but no
workflow in this repository invokes it, so there is no execution record to cite.
Accepting a rollout run instead would be the same false inference this whole check
exists to prevent. The refusal lifts once a dispatch-only lane that runs
`k8s/rollback.sh --teardown` exists, publishes its execution attestation, and is
registered in `teardown.K8S_TEARDOWN_WORKFLOWS`; its run is then supplied via
`SUPERPLANE_LIVE_K8S_TEARDOWN_RUN_ID` / `_ATTEMPT` and held to the same step-level
standard as the Terraform lanes.

Two further producers are required and do not exist yet, so the check fails closed
on them as well rather than accepting a caller-authored substitute:

| Missing producer | What it must publish | Registry / input |
|---|---|---|
| A lane that runs `record_pre_teardown` against the live deployment | its receipt, as a `superplane-pre-teardown-receipt` artifact | `teardown.RECORDER_WORKFLOWS`, then `SUPERPLANE_LIVE_RECORDER_RUN_ID` / `_ATTEMPT` |
| `superplane-infra-apply.yml`, `superplane-k8s-deploy.yml`, `superplane-infra-destroy.yml` | a `superplane.u1l1.execution-attestation/1` document per run, uploaded as `superplane-apply-attestation` / `superplane-rollout-attestation` / `superplane-destroy-attestation` from the step that did the work | downloaded into `SUPERPLANE_LIVE_ATTESTATION_DIR` |
| A `k8s/rollback.sh --teardown` lane | `superplane-k8s-teardown-attestation` | `teardown.K8S_TEARDOWN_WORKFLOWS` |

Each attestation must carry `schema`, `run_id`, `run_attempt`, `revision`,
`workflow`, `repository`, `account`, `environment`, `region`, `action` and
`resources`; the Terraform lanes additionally `state_bucket` and `state_key`, and the
Kubernetes lanes additionally `cluster` and `cluster_arn`. The refusals name the
artifact, the publishing step, the registry key and every missing field, so the next
author does not have to guess. The `run_attempt` a document states is checked against
GitHub's per-attempt record rather than believed, so the upload must happen **within
the attempt that did the work** — a `actions/upload-artifact` step in the same job is
what satisfies this; re-attaching evidence from a later attempt or a separate run does
not. **These artifacts are not fabricated here and their
absence is not worked around.** Until all three producers exist, U1-L1 acceptance 4
is genuinely unprovable, not passing — and no live pass is claimed.

The checker is observational. Its commands are allowlisted by full shape, so no
deploy, delete, apply, destroy, migration or feature change is reachable — including
`deploy-all.sh --superplane-only`, which despite its name still runs shared platform
phases. Running it does not authorize the operations it observes.

### Step 1 — record the receipt BEFORE the teardown

This step is not optional and cannot be reconstructed afterwards. While the
deployment still exists, with AWS credentials for the target account and kubectl
pointed at the named cluster, run from the repository root:

```sh
export PYTHONPATH="$PWD/modules/domain-apps/superplane"
export SUPERPLANE_LIVE_ENVIRONMENT='embark1/dev'
export SUPERPLANE_LIVE_TF_ENVIRONMENT='dev'
export SUPERPLANE_LIVE_CLUSTER='<EKS cluster the rollout targeted>'
export SUPERPLANE_LIVE_DEPLOY_RUN_ID='<successful superplane-infra-apply run ID>'
export SUPERPLANE_LIVE_DEPLOY_SHA='<40-hex revision that apply ran>'
export SUPERPLANE_LIVE_INVENTORY_FILE='/absolute/new/path/pre-teardown-receipt.json'
python3 -c 'import os; from superplane_acceptance.teardown import record_pre_teardown; record_pre_teardown(os.environ)'
```

The recorder verifies the deploy run the same way the verifier does — right workflow,
this repository, stated revision, deliberate dispatch, and a `Terraform Apply` step
that actually executed — then derives the inventory at that revision and performs the
same read-only lookups, recording what it actually saw. It writes nothing unless
**every** derived resource was observed `present`: a resource it could not see is a
resource whose later absence proves nothing, so an incomplete observation is a
refusal rather than a receipt with gaps. No undeploy run is needed; the teardown has
not happened yet.

Supported types are `aws_iam_role`, `aws_ssm_parameter`, `aws_ecr_repository` and the
Kubernetes kinds U3's manifests render; an entry of any other type is refused rather
than skipped. An optional `arn` per entry is attributed too. Inline role policies, the
role-policy attachment and ECR lifecycle policies are deliberately not separate
entries: each is deleted with its parent role or repository, which **is** an entry.

Do not hand-write this file. A hand-written resource list is refused by schema, with
the reason stated: it cannot establish that the resources were ever present. Running
the recorder locally is also not sufficient on its own — step 2 authenticates the
receipt against GitHub's record of the artifact a registered recorder lane uploaded,
which is one of the missing producers above.

### Step 2 — verify absence AFTER the teardown

Requires the authorized deploy, rollout and undeploy that have now happened, all three
run IDs and revisions, the receipt from step 1, and the execution-attestation archives
those runs published — downloaded (`gh run download`) into one directory. With AWS
credentials for the target account and kubectl pointed at the named cluster, run from
the repository root:

```sh
export SUPERPLANE_LIVE_ENVIRONMENT='embark1/dev'
export SUPERPLANE_LIVE_TF_ENVIRONMENT='dev'
export SUPERPLANE_LIVE_CLUSTER='<EKS cluster the rollout targeted>'
export SUPERPLANE_LIVE_DEPLOY_RUN_ID='<successful superplane-infra-apply run ID>'
export SUPERPLANE_LIVE_DEPLOY_SHA='<40-hex revision that apply ran>'
export SUPERPLANE_LIVE_ROLLOUT_RUN_ID='<successful superplane-k8s-deploy run ID>'
export SUPERPLANE_LIVE_ROLLOUT_SHA='<40-hex revision that rollout ran>'
export SUPERPLANE_LIVE_UNDEPLOY_RUN_ID='<successful superplane-infra-destroy run ID>'
export SUPERPLANE_LIVE_UNDEPLOY_SHA='<40-hex revision that destroy ran>'
export SUPERPLANE_LIVE_INVENTORY_FILE='/absolute/path/pre-teardown-receipt.json'
export SUPERPLANE_LIVE_ATTESTATION_DIR='/absolute/existing/dir/attestations'
export SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE='/absolute/new/path/u1-teardown.json'
python3 -m pytest modules/domain-apps/superplane/tests/acceptance/test_u1_live.py -q --tb=short
```

The rollout run is required because the Terraform apply creates none of the Kubernetes
objects; without it the Kubernetes half of the inventory would have no execution behind
it at all. Its `Apply` step is `if: inputs.dry_run == false`, so a dry run concludes
green having applied nothing — which is why the step list is checked per attempt.

`SUPERPLANE_LIVE_ATTESTATION_DIR` holds files on the caller's own filesystem, and that
is deliberately not where their authority comes from: each archive is hashed and
compared against the digest GitHub recorded for that run's artifact, so an edited or
hand-made archive fails authentication rather than passing.

GitHub reads use the existing authenticated `gh` CLI. No ADP token is needed. Every
input is required: a missing one fails with `BLOCKED` and the test does **not** skip,
so no acceptance criterion can be silently unchecked. Use an existing output
directory and a new evidence filename; an existing file or symlink is never
overwritten. `SUPERPLANE_LIVE_K8S_TEARDOWN_RUN_ID` / `_ATTEMPT` and
`SUPERPLANE_LIVE_RECORDER_RUN_ID` / `_ATTEMPT` are read when supplied but cannot yet be
satisfied — see the known gap above. Omitting them blocks; it never skips a check.

### What the record establishes, and what it does not

On a complete pass the evidence file records the selected target, the verified
account/cluster identity, all three operation receipts with revisions, run URLs,
attempts and the specific steps proven to have executed, the execution attestations as
**GitHub's** records of them (artifact id, name, digest, `created_at`, size, run and
attempt) rather than as the documents' own self-description, the revision the inventory
was derived at with a hash per source file, the Terraform state bucket and key the
backend configuration established, the derived and observed inventory counts with the
receipt's hash, the receipt's own provenance (observation time, cluster ARN, and the
recorder hash **this check computed** at the recorder revision), the Kubernetes
DELETED/RETAINED lifecycles, the per-resource observation outcomes, timestamps and the
verifier's hash. It carries no credential
and no raw credential-bearing response. **No artifact is written on a failed or
partial check**, so a half-finished run cannot leave something that later reads as
acceptance. Injected transports produce `offline-fixture` records that the live
entry point refuses to publish.

The record is labelled `u1_acceptance: incomplete` and names what it does not
establish: browser gating or enabled Superplane behaviour, unset-configuration
defaults, the feature API contract (the separate check above), SkyPilot GPU clusters
or database contents this module never created, and the Secrets Manager secrets the
deploy takes by reference only. Per the destroy lane, those secrets and everything
platform-owned survive by design and are correctly not in the inventory.

A live pass still needs an authorized deploy and teardown with their execution
evidence and a named cleanup owner. Existing embark1/dev CI authorization is not
teardown authorization. Until that observation exists, U1-L1 and #5067 stay open.

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
attempts. The teardown regressions add incomplete/mock/oversized inventories,
hand-written resource lists and receipts whose deploy, cluster, source hashes or
observation window do not match what the run established, entries not observed
present before teardown, foreign and platform-owned identities, still-present
resources, denied, throttled, expired and malformed reads, a mismatched account,
cluster or kubectl context, missing or out-of-order operation receipts, runs nobody
dispatched or lacking an attempt, jobs and steps belonging to another run or attempt,
the empty-state destroy whose plan/ownership/destroy steps are `skipped` while the run
concludes green, an apply that never applied, a dry-run rollout, a missing Kubernetes
deletion lane, a resource removed from later source but created by the deployed
revision, the recorder's own refusals, and rejection of every mutating command shape.
The attestation and recorder-provenance regressions publish artifact records in the
real API's shape — asserted field-for-field against it by a drift guard, so a fixture
cannot reintroduce a field GitHub does not send — and then break exactly one thing
each: an unregistered producer, an expired or duplicated
artifact, a run binding naming another run or another head, a per-attempt record that
is unreadable, reports another attempt, another run, a foreign head, a failed
conclusion or no window, an artifact left behind by an earlier attempt of the same run,
a digest that is absent, malformed
or does not match the bytes, an archive that was edited without being republished, an
archive that is not one JSON document or is oversized, an upload GitHub stamped outside
the closed deploy-to-teardown window, a publishing step that was never proven to
execute, every individual required field missing, every field disagreeing with an
independently established fact (account, environment, region, state bucket/key, an
unresolved `ACCOUNT_ID` placeholder, run id, revision, workflow, repository, schema,
plan action, cluster), an attestation omitting a derived identity or naming another
namespace, an offline-fixture receipt offered on the live path, and a forged
`recorder_sha256` — including the exact `forged-unvalidated-value` the review named, and
a hash taken at the wrong revision. A positive control republishes an artifact record
carrying an invented `run_attempt` and asserts verification still matches, proving the
attempt is taken from GitHub's per-attempt record and not from anything a producer can
write. Their injected
transports produce `offline-fixture`/`matched` results, not `live`/`passed` evidence. Subprocess regressions verify that each explicit live
pytest command fails, without skipping, before any network or AWS access when its
inputs are absent.

U12's real baseline acceptance remains a separate prerequisite tracked by #5067.
Offline authoring of these checks closes none of the live criteria: the API
observation, the U1-L1 teardown observation and the U6 check must each actually be
executed against the real boundary by someone authorized to do so.
