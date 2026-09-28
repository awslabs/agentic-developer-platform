# CLI uplift delivery and rework audit — 17 September 2026

The strongest recurring cause in this sample is a gap between required behavior,
the executable path delivered, and what the tests actually assert. Several of
the most expensive failures violated requirements already written before
development. More detailed issues alone would not have prevented them.

There are also failures owned by coordination and the platform: reviews arrived
after merge, reviewers disagreed about the same revision, one review queried the
wrong deployment, and explicit continuation requests never reached a developer.
Those should not be counted as developer implementation time or blamed on issue
wording. The template improvements in PR #5313 help, but cannot enforce runtime
completion or merge decisions.

## Scope and method

This is a targeted audit of the CLI uplift discussed with the user, not a random
sample or a platform-wide estimate of developer quality.

| Story | Delivery PR | Capability |
|---|---|---|
| [#5185](https://github.com/aws-e/adp/issues/5185) | [#5188](https://github.com/aws-e/adp/pull/5188) | Shared CLI foundation and native Cognito login |
| [#5181](https://github.com/aws-e/adp/issues/5181) | [#5179](https://github.com/aws-e/adp/pull/5179) | Bedrock setup and hierarchical routing |
| [#5182](https://github.com/aws-e/adp/issues/5182) | [#5204](https://github.com/aws-e/adp/pull/5204) | Personal AWS connection |
| [#5183](https://github.com/aws-e/adp/issues/5183) | [#5201](https://github.com/aws-e/adp/pull/5201) | Administrator GitHub App setup |
| [#5184](https://github.com/aws-e/adp/issues/5184) | [#5203](https://github.com/aws-e/adp/pull/5203) | User GitHub connection |
| [#5199](https://github.com/aws-e/adp/issues/5199) | [#5221](https://github.com/aws-e/adp/pull/5221) | Repeatable live evaluation |

Followed the evaluation through execution issues
[#5242](https://github.com/aws-e/adp/issues/5242),
[#5248](https://github.com/aws-e/adp/issues/5248) and
[#5256](https://github.com/aws-e/adp/issues/5256), and their linked fixes.

Inspected issue bodies and available edit history, timeline links, delivery PRs,
commits, review/status comments and historical source. For #5181–#5184, creation
text and refinements at approximately 10:57 UTC on 15 September precede the
relevant implementation/integration work; #5179 already existed before #5181
and was explicitly to be reused. #5199's original 14:57 UTC body already required
E01–E15, live EC2 execution, independent usage evidence, durable recovery and two
fresh full passes. No edit-history nodes were returned for #5185, so its current
body is not treated as independent proof of every original sentence.

Evidence has three levels:

- **Reproduced in this audit:** five bounded offline counterexamples executing
  historical function bodies. The accompanying script pins the source revisions.
- **Inspected source/history:** call paths, source changes, timestamps and the
  relationship between a review finding and a subsequent commit.
- **Historical reported observation:** live results, AWS observations and test
  counts in linked reports. These were not rerun live in this audit.

Review verdicts in these PRs were posted as ordinary comments; the formal review
collections were empty when inspected. A missing formal review therefore does
not prove no review occurred, and a blocking comment does not itself establish
that branch protection would stop a merge. For comments edited from progress to
completion, use the updated timestamp, not placeholder creation time.

No AWS resources, live product sessions or developer dispatches were created for
this audit. Historical defects reproduced here are not claims about today's
deployed state. Commit counts are not rework counts: they include WIP, formatting,
planned development, merges and actual corrections.

## Story-level findings

### 1. #5199 / #5221: implementation completeness was inferred from tests that bypassed it

The original issue already required real scripts on clean EC2, not merely an
evaluation registry. The
[first delivery](https://github.com/aws-e/adp/issues/5199#issuecomment-5685952919)
claimed implementation complete while correctly leaving live acceptance open.
That distinction was insufficient: implementation itself was incomplete.

| Revision / handoff | Observed result | What changed afterward |
|---|---|---|
| `152454bff1`, first delivery | Workflow entry point supplied an empty stage mapping; all 15 cases stayed not-run. A resumed run could report full acceptance from old results despite a newly failed preflight. Recovery lacked equivalent guards and durable cross-run state. | Developer explicitly [reproduced all five findings](https://github.com/aws-e/adp/issues/5199#issuecomment-5686416296). |
| `8434056923`, claimed all five fixed | Stage failures now vetoed acceptance and recovery had trusted-ref/account guards. The remaining areas were only partially fixed: callable stages still had no default implementations for nine journeys, and the remote worker they invoked was not shipped. | [Second review](https://github.com/aws-e/adp/issues/5199#issuecomment-5688060197) identified ten concrete defects despite 224 reported passing offline tests. |
| `ee3b8b45b0` onward | Commit explicitly shipped previously absent on-instance scripts. Later commits moved tests toward real transports, repaired durable mutation/recovery wiring and added journeys. | Meaningful implementation work, not ten arbitrary changes of reviewer preference. |
| `3acc9b57cc`, merged 16 September 06:03 UTC | Final PR was narrowed to a runnable EC2 login checkpoint. | A deliberate scope checkpoint. It did not satisfy the original full E01–E15 story. |

The second review was not simply repeating all five earlier comments. Two fixes
were accepted; other fixes addressed only the first layer of a broken path,
exposing defects beneath it. Examples:

- Registering a callable stage did not create its remote script or its journey
  implementation. Tests replaced `run_worker`/`journey`, the very boundary that
  needed to be proved.
- Persisting state at the beginning and in `finally` did not preserve resource
  mutations when the process died before `finally`. Recovery also used a
  different configuration without the state bucket and swallowed restoration
  failure.
- E15 counted its own initial not-run status and demanded cleanup before the
  cleanup stage. Success was impossible even if other cases passed.
- Expected release hashes came from the same served files being checked. A
  stale release could agree with itself.
- The invented `/api/cli/discovery` route and unbound role references could
  prevent any real journey from starting.

**Root cause:** incomplete implementation plus tests that proved internal
helpers in isolation instead of the shipped entry point and its evidence.
The issue's breadth made it easier to build a framework around all fifteen rows
before proving one complete path. A first runnable checkpoint should have been
required earlier, while retaining the full acceptance boundary.

**Specific prevention:** run the actual workflow command with its normal stage
factory and built worker bundle in an offline transport-controlled test. Missing
worker, missing driver or absent required binding must fail. Then run one live
EC2 journey before expanding the matrix. Unit tests remain useful but cannot
stand in for that path.

### 2. #5185 / #5188: incomplete delivery and unresolved findings crossed the handoff

The initial developer run hit a Bedrock `internalServerException`. Its PR at
`2e626c7343` contained only a design document; a review of that revision did not
cover the executable code added later. This is a runtime completion/handoff
problem, not proof that the developer spent the whole elapsed interval coding.

At `1f279a9`, a
[review](https://github.com/aws-e/adp/pull/5188#issuecomment-5680563416)
found two blockers: `save_session()` erased an existing `identity_pool_id`, and
per-IP login throttling keyed on the load balancer's address. A
[second review at `04c9c07d`](https://github.com/aws-e/adp/pull/5188#issuecomment-5681021987)
executed reproductions and found the same code unchanged. The PR merged at
14:10:53 UTC with the config-clearing code still present at head `3d22b4a039`.
The audit's offline probe independently reproduces that config loss.

The test checked preservation of an unrelated config key, so it could pass
while the load-bearing key was destroyed. The missing state was a realistic
existing installation, rather than another empty-config happy path.

The review process then added avoidable confusion:

- A separate [approval](https://github.com/aws-e/adp/pull/5188#issuecomment-5682471912)
  for `04c9c07d` reported no blockers, conflicting with reproduced findings.
- A [post-merge review](https://github.com/aws-e/adp/pull/5188#issuecomment-5682465343)
  said the code was not deployed, based on the wrong gateway.
- The [correction](https://github.com/aws-e/adp/pull/5188#issuecomment-5682721960)
  verified the selected deployed dev gateway and reversed that exposure claim.
  The defect was tracked as [#5200](https://github.com/aws-e/adp/issues/5200).

Later [#5262](https://github.com/aws-e/adp/issues/5262) exposed a separate
deployment gap: Terraform declared `AdminRespondToAuthChallenge`, but the live
gateway role lacked it. Login for an already-confirmed user did not exercise
first-password or MFA challenges. This cannot be fixed just by writing another
application-level login test.

**Root causes:** realistic-state coverage, runtime delivery gating, unresolved
review disposition, target provenance and deployment ownership. Only part of
this belongs in the issue template.

**Specific prevention:** seed meaningful existing config; exercise actual
fresh-password and configured-MFA states at the deployment phase; bind review
and evidence to a revision/base/target; explicitly reconcile findings before
merge; preserve an interrupted run as incomplete even if it produced a PR.

### 3. #5183 / #5201: an explicit ownership rule lacked a conflicting-input case

The pre-development issue explicitly said: “Do not silently substitute a
personal app for an intended organization-owned app.” At `18ff18db70`,
`owner_choice()` accepted `--owner user --github-org fixture-org`, returned
personal ownership and never read the organization argument. It warned on
stderr but did not reject the contradiction. The offline audit reproduces this.

The [review](https://github.com/aws-e/adp/pull/5201#issuecomment-5684143922)
reported one blocker and four nonblocking correctness findings, despite 50
passing new tests. The branch's final four commits contain implementation,
tests/docs and formatting, not a subsequent fix for this finding.

The PR merged at 16:26:11 UTC. The detailed verdict was posted at 16:37:16;
the progress comment became its final report at 16:38:53. The earlier placeholder
creation time must not be used to claim that this final verdict was available
before merge.

**Root cause:** missing input-combination coverage, plus merge/review timing.
The business rule was clear. The template can prompt a small decision table,
but a developer still has to translate the rule into validation before mutation.

**Specific prevention:** check default ownership, explicit organization,
explicit personal ownership and conflicting flags through the CLI parser and
real ownership resolver. Contradiction must fail before register-start. Do not
claim that adding another copy of the existing prose would solve this.

### 4. #5184 / #5203: mostly successful delivery with state-specific gaps

This is a useful counterexample to a blanket “agents deliver poor work” claim.
The [review](https://github.com/aws-e/adp/pull/5203#issuecomment-5683929123)
approved the implementation without blockers. In particular, live repository
provenance prevented cached configuration from being called verified access.

Two nonblocking defects were found and are reproduced offline here:

- Every HTTP 503 was classified as “GitHub App missing,” including an
  infrastructure outage. The shared transport's weak error contract contributed
  to this inference; keeping within a file-ownership boundary did not make the
  inference correct.
- `--dry-run` cleared saved pending state when an existing installation already
  granted the repository. The fresh-install dry-run test did not cover reuse.

Merging the administrator GitHub helper required retaining both helpers in
shared packaging lists. Commit `9eab2009bb` and the
[integration report](https://github.com/aws-e/adp/pull/5203#issuecomment-5684071448)
describe that repair and 181 passing integration tests. A
[follow-up review](https://github.com/aws-e/adp/pull/5203#issuecomment-5684027181)
after the base changed was legitimate integration work, not necessarily a repeat
of resolved findings. The PR history does not establish fixes for the two
nonblocking defects above.

**Root causes:** state-dependent behavior and a producer/consumer error-contract
gap. Shared-file integration is a separate, expected cost of parallel work.

**Specific prevention:** dry-run assertions cover existing and pending state as
well as fresh state; error handling distinguishes a missing configuration from
a temporarily unavailable service. Give the shared transport/packaging owner
explicit integration responsibility instead of treating file boundaries as a
reason to leave required behavior incorrect.

### 5. #5182 / #5204: an unrelated harness was presented as validation

The issue required personal AWS CLI setup and canonical CLI/UI records. Initial
docs pointed at `test-cli-routing.py --provision direct|handoff`, which runs
`adp admin bedrock connect`, not `adp aws connect`.

The coordinator [identified the mismatch](https://github.com/aws-e/adp/pull/5204#issuecomment-5683436152).
Commit `e398c336fd` and the
[correction](https://github.com/aws-e/adp/pull/5204#issuecomment-5684071133)
accurately deferred the missing personal-AWS live adapter. That fixed the claim
and documentation; it did not create personal-AWS live acceptance.

Later live use exposed a cross-route identity defect:
[#5265](https://github.com/aws-e/adp/pull/5265) fixed create/list/delete disagreeing
about the organization for native Cognito tokens without an org claim.
A successful connect could produce a credential invisible to list/delete.
Reusing canonical APIs was necessary but insufficient to prove their composition
with this newly supported caller. This audit does not attribute every older
backend defect to the CLI story.

Reviews also disagreed about the merged sibling scope at `13b59cd4`:
[request changes](https://github.com/aws-e/adp/pull/5204#issuecomment-5684636622)
versus [approve with disclosure](https://github.com/aws-e/adp/pull/5204#issuecomment-5684663458).
The actual merge order was #5203 then #5204, four seconds apart. The
dependency/base concern was legitimate; duplicate contradictory verdicts made
the acceptance signal harder to interpret.

**Root causes:** evidence attached to the wrong command, integration identity
coverage, and coordination. The core feature requirement was not absent.

**Specific prevention:** evidence records the exact user command and backend
path. One native identity must create → list → fresh-verify → delete → confirm
absence, including a token without optional org claims; browser parity needs
an actual browser-created resource if that is the promised result.

### 6. #5181 / #5179: retain the useful delivery pattern

The [EC2 validation report](https://github.com/aws-e/adp/issues/5181#issuecomment-5681542619)
provides positive evidence: direct provisioning and separate-admin handoff,
eight real Claude/Codex invocations, separate request and usage IDs, idempotent
reuse, account-mismatch refusal, and cleanup observations. An initial private
STS endpoint failure was repaired in the reusable worker before two fresh
provisioning runs passed.

The [hierarchy report](https://github.com/aws-e/adp/issues/5181#issuecomment-5680956399)
also separated personal routing success from hosted ingress failure
(`Missing chat run owner or tenant`). Public release/native login and cloud-agent
acceptance remained open. This is historical reported live evidence, not a fresh
certification by this audit.

**Lesson:** a narrow real journey with explicit evidence and limitations is more
useful than an unqualified completion claim. The later choice of destination
account `938500344975` does not retroactively make the previously authorized
`605440105851` runs erroneous. Account selection must remain configurable.

## Why the live execution loop kept expanding

| Evidence | Actual cause | Responsible improvement |
|---|---|---|
| [#5243](https://github.com/aws-e/adp/pull/5243) | Real Actions job used `python` before setup-python on ARC. 310 offline tests did not execute that job sequence. | Validate the real runner bootstrap path, not just Python modules. |
| [#5250](https://github.com/aws-e/adp/pull/5250) | Missing fixture bindings, missing credentials path and secret-field mismatch. | Check the resolved workflow configuration against what workers consume. |
| [#5251](https://github.com/aws-e/adp/pull/5251) | Retrying `sts:TagSession` and changing target trust did not overcome the source role's permission boundary. | Validate the whole caller → assume-role chain, including boundary and optional action defaults. |
| [Restart audit](https://github.com/aws-e/adp/issues/5242#issuecomment-5695121690) | Requests at 07:16 and 09:14 UTC were skipped as `idempotency_merged_pr`; a merged fix PR was mistaken for assignment completion. | Runtime must distinguish explicit continuation from duplicate event delivery. There was no active developer to monitor in this interval. |
| [Session handoff](https://github.com/aws-e/adp/issues/5256#issuecomment-5696617994) | Tokens were emitted through redacted evidence, stored as `<redacted>`, then reused as credentials. | Keep private execution state separate from exported evidence; test the complete producer → serializer/redactor → consumer path. |
| [#5274](https://github.com/aws-e/adp/issues/5274) | Two client completions, but one generic recent successful usage row satisfied billing correlation. | Distinct request correlation and a missing-Codex-row negative case through the real predicate. |
| [Final outcome audit](https://github.com/aws-e/adp/issues/5256#issuecomment-5704212575) | Six reported passes included incomplete MFA, browser parity and recovery/two-run proofs. Access gaps were described as wholly external although coordinator access had been verified. | Separate implemented, executable and accepted; name the actual setup owner; qualify composite evidence. |

The login checkpoint eventually produced
[two verified fresh runs](https://github.com/aws-e/adp/issues/5242#issuecomment-5695933385)
with `partial=true`, `full_acceptance=false`, and independently observed cleanup.
That was a real useful outcome.

For the broader execution, the historical coordinator audit counted 17
dispatches, four successful selected-suite runs, and two final full runs that
failed overall with 6 passed / 9 blocked. This is a count of workflow outcomes,
not developer hours or seventeen distinct defects. Some failures exposed the
next issue only after the preceding one was fixed. Other dispatches could have
been avoided by resolving known setup first.

The [developer's final report](https://github.com/aws-e/adp/issues/5256#issuecomment-5702022612)
correctly kept `full_acceptance=false`, but its stronger claim that only external
grants remained was unsupported. E02 lacked configured-MFA evidence; E13 checked
browser wire types without the browser-created journey; E15 said
`resumed=false` and `this_run_contributes_only=true`. E08 also had an open
assertion defect. Stable IDs already existed; they did not prevent incomplete
evidence from being assigned to a complete-sounding row.

## Independent offline counterexamples

Run from a checkout with the historical Git objects available:

```bash
python3 docs/analysis/cli-uplift-rework/probe_historical_failures.py
```

| Pinned source | Input | Observed in this audit |
|---|---|---|
| `3d22b4a039`, `adp_common.py::save_session` | Existing nonempty identity pool; synthetic login result | Pool becomes empty; temporary local config only. |
| `18ff18db70`, `adp-github-admin.py::owner_choice` | Personal owner plus explicit GitHub org | Returns personal ownership without rejecting contradiction. |
| `bf45250531`, `adp-github.py::platform_app_missing` | Infrastructure HTTP 503 | Classified as missing App. |
| Same revision, real `connect`/reuse/state-clear path | Dry-run, existing installation, saved pending request | Writes empty pending state. |
| `3c88c8a2`, `personal_inference.py::_usage_record` | Codex marker, only an unrelated recent successful Claude row | Returns the Claude row. The real caller also invokes the selector only once with the Claude marker. |

The probes use historical function bodies and controlled external/state
boundaries. They do not exercise a real CLI installation, network, cloud or full
workflow. In particular, the usage probe demonstrates the selector defect; it
does not claim to run the entire inference case or diagnose the underlying
product's missing usage write.

## Improvements tied to the causes

### Apply now in the template and authoring/developer/reviewer guidance

Retain the six-section template and one acceptance table. Avoid making every
small issue a large specification. Add the following only where the behavior
needs it:

| Change | Concrete requirement | Why it would help this sample |
|---|---|---|
| Trace acceptance to execution | Each critical AC names the real command/entry point, implementation location, assertion and evidence source. Developer supplies the implemented map; author marks proposed paths honestly. | A callable stage or Bedrock harness cannot stand in for a missing personal-AWS journey. |
| Require a discriminating negative case | Identify a plausible wrong result that must fail the critical assertion; exercise the production predicate. | Missing Codex usage, stale release, wrong account or undeleted resource cannot satisfy a nearby success. |
| Cover relevant state and input combinations | Small decision table for fresh/existing/pending, contradictory flags, optional identity claims and interrupted/resumed behavior. | Catches config erasure, personal ownership fallback, dry-run mutation and invisible connections. |
| Name data and identity handoffs | State who produces a field/session/resource ID, how it travels, who consumes it and how cleanup authenticates after interruption. | Prevents redacted credentials and create/list/delete identity disagreement. |
| Use a first runnable checkpoint for broad automation | One real EC2 path through installation, intended operation, evidence and cleanup before expanding the full matrix. | Makes absent workers, runner setup and binding gaps visible early. The checkpoint must remain partial. |
| Keep compound acceptance explicit | Subclaims for E02 password/MFA, E08 Claude/Codex, E13 each creation direction, E15 two runs/interruption/resume/cleanup. | A per-run contribution or API type check cannot be called the whole acceptance row. |
| Give setup and rollout an owner | Required grant/binding, observed readiness, retained vs per-run setup, applying workflow and phase owner. | A correct Terraform declaration is not live IAM, and a worker's access denial is not proof the coordinator cannot supply setup. |
| Make fix reports resolve the reproduction | Finding → changed revision → same reproducer result → affected regression checks. Partial fixes stay partial. | “Registered stages” does not resolve “workflow runs the intended journeys.” |

The first version of #5313 already improved completion ownership, prerequisites,
phase-specific evidence and conflicting authoring instructions. This audit adds
the execution path, negative-case, state/handoff and composite-acceptance details.
Those are grounded refinements, not a claim that more prose guarantees success.

### Separate runtime and coordination changes still required

These are recommendations, not implemented by this documentation PR:

| Priority / owner | Change | Acceptance of the improvement |
|---|---|---|
| P0 — delivery-runtime owner | Interrupted/failed developer runs remain incomplete; a PR artifact alone cannot signal implemented delivery. | Reproduce the docs-only failed-run case; it is visibly incomplete and does not request an implementation verdict. |
| P0 — delivery-runtime owner | Explicit continuation can proceed after a fix PR merges; duplicate transport events remain deduplicated. | A new continuation executes once; replaying its event executes zero additional times. |
| P0 — coordinator / merge integration owner | Reconcile known blocking findings against the current revision/base and merge state; do not treat an unrelated approval or green CI as a resolution. | Unchanged reproduced blocker cannot be silently cleared by a later contradictory comment. Late review is routed to a concrete follow-up rather than a fictitious open-PR gate. |
| P0 — evaluation owner | Keep implementation gaps distinct from fixture-blocked checks and partial evidence; composite pass needs every required subclaim. | Removing Codex correlation or interruption proof makes the required row incomplete even when other subclaims pass. |
| P1 — environment owner | Prepare and verify fixture/role/binding contracts using the actual ARC and EC2 identities, with a named setup handoff. | One fresh supported run gets through prerequisites without manual edits; repeat cleanup works with the intended identity. |
| P1 — coordinator / reviewer | Review requests carry selected deployment/account and revision evidence; reject mismatched targets and stale scope assumptions. | Wrong gateway or changed sibling/base cannot produce an unqualified verdict about the selected release. |

No extra approval persona, broad permission grants, generic testing framework or
new mandatory CI job is implied. Use the existing workflow and checks first;
enforce only controls that have an identified failure to prevent.

## How to judge whether this helped

Apply the revised contract to #5282 and a small next batch of stories. Record
from the existing issue/PR history:

- Required behavior missing at first review, distinguished from new scope.
- Findings resubmitted without their reproduction being fixed.
- Time to the first real usable journey, separately from fixture/approval waits.
- Integration and deployment failures that existing readiness evidence missed.
- False or overstated pass claims; explicit continuations silently skipped.

Compare those outcomes with this audit. Do not optimize for fewer review comments
or more passing unit tests: suppressing valid findings would improve those
numbers while making delivery worse. The practical target is fewer preventable
implementation and handoff failures on the first delivery, with legitimate new
discoveries still reported honestly.
