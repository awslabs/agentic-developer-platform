# Live Run-Control Evaluation — Fixture Config and Harness

**Subsystem:** Agent live control (gateway control routes + agent control listener)
**Harness:** `platform/scripts/agent-control-eval.py`
**Story:** #3960 (S1, epic #3959) · **Evaluation:** #3967 (Wave 1)

## Why this runbook exists

The Wave 1 evaluation is run by an operator against a live account, and its
result is the evidence that closes #3967. The harness cannot be run in CI: it
needs a real credential and an operator-created isolated fixture, so everything
about *how the fixture is described* has to be written down rather than
discovered by reading the script.

This runbook is that description. It is also the thing that makes the harness's
refusals interpretable — when it exits 2 saying `fixture_isolated must be exactly
true`, the answer is on this page and it is not "enable the flag on dev".

The config contract lives here and only here, deliberately: two documents
describing the same JSON is how the second one goes stale. The example below is
parsed and validated by CI for the same reason.

## The one rule that outranks the evaluation

`FEATURE_AGENT_CONTROL_ENABLED` may be enabled **only** in an operator-created,
run-bound, disposable fixture (DP-INV-1). Ordinary gateway, worker and SPA flags
stay off.

If fixture setup fails, the evaluation is **NOT RUN** — nonzero, no evidence.
Enabling the flag more broadly to get the harness to run does not produce a
weaker pass; it produces an invalid one, and it leaves a control listener
reachable on a shared environment. The harness enforces this where it can
(`fixture_isolated` must be the literal JSON `true`, because the string
`"false"` is truthy in Python and a `bool()` check would have accepted it), but
the harness only sees what the config claims. The claim has to be true.

## Prerequisites

| Requirement | Value / how |
|---|---|
| Live target | `879318057152` / `dev` / `embark1` |
| Credential label | `adp-embark1` |
| Credential path | The supported credential path. Never in the config file, never echoed, never committed. |
| Isolation | Ingress policy applied **before** the listener starts |
| Deployed revision | Verify the *deployed* digest, not that a build went green |
| Python deps | `pip install 'boto3>=1.34' httpx` |

Development and PR tests need no AWS deployment credential. Merged and CI-green
does **not** close #3967 — only a complete live run with all ten checks passed
and cleanup successful does.

## Running it

From the repository root:

```sh
export CONTROL_EVAL_CONFIG=/path/to/your/fixture.json      # not in this repo
export CONTROL_EVIDENCE_DIR=./test-results/agent-control    # gitignored

python3 platform/scripts/agent-control-eval.py \
  --wave 1 --config "$CONTROL_EVAL_CONFIG" --evidence-dir "$CONTROL_EVIDENCE_DIR"

jq -e '.failed == 0 and .skipped == 0 and .not_run == 0
       and .passed == .required and .cleanup_ok == true' \
  "$CONTROL_EVIDENCE_DIR/result.json"

check() {
  jq -e --arg id "$1" '.checks[$id] | .status == "passed" and (.evidence | length > 0)' \
    "$CONTROL_EVIDENCE_DIR/result.json"
}
```

`--dry-run` validates the config and the live preconditions (account, table key
schema) and stops before contacting the control path. Run it first: it is the
cheap way to find a wrong account or a typo'd table without creating anything.

`--evidence-dir` defaults to `./test-results/agent-control`, which is
gitignored — `result.json` names environment identifiers and must not be
committed. `--output-dir` is accepted as a deprecated alias so an operator
following an older note is redirected rather than stopped.

### Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | All checks passed, cleanup confirmed | Attach `result.json` to #3967 |
| 2 | Config error — nothing was contacted | Fix the file and rerun; no state to unwind |
| 3 | Precondition failed — wrong account, wrong key schema, no client | Fix the target; **do not** relax the check |
| 4 | One or more checks failed or could not run | Read the per-check `message`; a `not_run` names its missing prerequisite |
| 5 | Checks passed but cleanup did not complete | Treat as failure and clean up by hand — a fixture left with a listener is the DP-INV-1 state |

Exit 4 with `not_run` is the important one: a check that could not run is never a
pass. "Could not look" and "looked and it was wrong" are reported differently
(`not_run` vs `failed`) because they need different actions from you, but
neither satisfies the gate.

## The fixture config

One JSON object. **No credentials of any kind** — a key whose name contains
`token`, `secret`, `password` or `credential` is rejected outright, because a
committed file carrying a credential is a leak that redaction cannot undo after
the fact. Bearer tokens are named indirectly, by the environment variable
holding them, under `identity_env`.

### Complete example

Values below are illustrative except `account_id`, which is the real Wave 1
target. Copy it outside the repo, then replace the run IDs, timestamps,
generation and digests with your fixture's actual ones.

> **This example is not runnable as-is.** `account_id` and `invocation_table`
> name the live dev account and the real webhook-events table, and every
> `cleanup_items` entry is issued as an unconditional DynamoDB `DeleteItem`
> against that table in a `finally` block — including when the run ends in
> NOT RUN. The placeholder `msg-0000…` keys match nothing today, so running it
> unedited deletes nothing; substituting **real** `event_id`/`arrived_at` values
> you do not own makes it delete production rows, and the table has no
> point-in-time recovery. Only ever list rows your own fixture created.

<!-- EXAMPLE-CONFIG-BEGIN -->
```json
{
  "account_id": "879318057152",
  "aws_region": "us-east-1",
  "environment": "dev-control-fixture-embark1",
  "fixture_isolated": true,
  "tenant_id": "org-fixture-0001",

  "gateway_url": "https://gateway.dev.internal",
  "flag_off_gateway_url": "https://gateway-flagoff.dev.internal",
  "invocation_table": "adp-dev-webhook-events",

  "live_run_id": "msg-0000000000000001",
  "arrived_at": "2026-09-12T10:00:00Z",
  "generation": 1,
  "terminal_run_id": "msg-0000000000000002",
  "terminal_arrived_at": "2026-09-12T10:05:00Z",
  "unknown_run_id": "msg-does-not-exist-0001",
  "aborted_run_id": "msg-0000000000000003",

  "command_id": "3f2b9c14-7d51-4e8a-9b02-5c6d7e8f9a0b",
  "oversize_bytes": 32768,
  "expected_output_digest": "sha256:2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
  "allowed_parity_fields": ["control", "registration", "state"],

  "source_digest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "deployed_digest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",

  "identity_env": {
    "owner": "CONTROL_EVAL_OWNER_SESSION",
    "nonowner": "CONTROL_EVAL_NONOWNER_SESSION",
    "other_tenant": "CONTROL_EVAL_OTHER_TENANT_SESSION"
  },

  "artifacts": {
    "provenance": "artifacts/provenance.json",
    "listener_auth": "artifacts/listener_auth.json",
    "token_lifecycle": "artifacts/token_lifecycle.json",
    "peer_probe": "artifacts/peer_probe.json",
    "fixture_task": "artifacts/fixture_task.json",
    "worker_unavailable": "artifacts/worker_unavailable.json",
    "transport_guard": "artifacts/transport_guard.json",
    "flag_parity": "artifacts/flag_parity.json",
    "journal_tests": "artifacts/journal_tests.json",
    "negative_tests": "artifacts/negative_tests.json",
    "harness_neutrality": "artifacts/harness_neutrality.json",
    "aborted_counters": "artifacts/aborted_counters.json",
    "vocabulary_parity": "artifacts/vocabulary_parity.json",
    "stats_schema_keys": "artifacts/stats_schema_keys.json",
    "neutral_contract": "artifacts/neutral_contract.json",
    "pause_boundary": "artifacts/pause_boundary.json",
    "pause_resume": "artifacts/pause_resume.json",
    "pause_expiry": "artifacts/pause_expiry.json",
    "wave2_preflight": "artifacts/wave2_preflight.json",
    "security_capture": "artifacts/security_capture.json",
    "teardown_verification": "artifacts/teardown_verification.json",
    "browser_control_run": "artifacts/browser_control_run.json"
  },

  "resource_teardown": [
    "/opt/adp/fixtures/teardown-control-fixture.sh",
    "--env", "dev-control-fixture-embark1"
  ],
  "resource_teardown_timeout_seconds": 600,

  "cleanup_items": [
    {"event_id": "msg-0000000000000001", "arrived_at": "2026-09-12T10:00:00Z"},
    {"event_id": "msg-0000000000000002", "arrived_at": "2026-09-12T10:05:00Z"},
    {"event_id": "msg-0000000000000003", "arrived_at": "2026-09-12T10:10:00Z"}
  ]
}
```
<!-- EXAMPLE-CONFIG-END -->

`platform/scripts/tests/test_agent_control_eval.py` parses the block above out of
this file and asserts it validates and covers every key the checks read, so this
example cannot drift from the harness without failing CI.

### Field reference

Required — the harness refuses to contact anything without all eight:

| Field | Meaning |
|---|---|
| `account_id` | Exactly 12 digits. Checked against `sts:GetCallerIdentity`; a mismatch is exit 3. There is no ambient default, because the default target for a control-plane evaluation would be whatever credential happens to be in the shell. |
| `environment` | Fixture environment name, recorded in the evidence so a later reader knows what was evaluated. |
| `fixture_isolated` | Literal `true`. See the DP-INV-1 note above. |
| `gateway_url` | Base URL of the flag-**on** fixture gateway. |
| `invocation_table` | DynamoDB invocation table. Its key schema is verified as `event_id` HASH + `arrived_at` RANGE before any write, because the control record is written onto the invocation row and a wrong key is either a silent no-op or a write onto an unrelated item. |
| `live_run_id` | `event_id` of the live fixture run. |
| `terminal_run_id` | `event_id` of a run whose row is terminal — the 410 case. |
| `tenant_id` | Tenant owning the fixture runs. |

Per-check inputs. Each is required by the checks listed; absent means those
checks are `not_run` (naming the field) while the rest still run:

| Field | Needed by | Meaning |
|---|---|---|
| `arrived_at` | W1-01 | Sort key of `live_run_id`. Both halves of the key, always. |
| `generation` | W1-01 | The worker generation the fixture is at, so a stale-generation response is recognisable. |
| `unknown_run_id` | W1-02 | An ID that does not exist. W1-02 compares its 404 against the wrong-tenant and same-tenant-nonowner 404s; if any of the three is distinguishable, the deployment has an enumeration oracle in a 404's clothing. |
| `command_id` | W1-02, W1-05, W1-06, W1-08 | A UUID used as the idempotency key on submitted commands. |
| `identity_env` | W1-02, W1-07 | Env var **names** for the `owner`, `nonowner` and `other_tenant` browser sessions. Unset vars are `not_run`, not a pass. |
| `terminal_arrived_at` | W1-06 | Sort key of `terminal_run_id`; used for the consistent read that confirms terminal teardown cleared the private fields. |
| `flag_off_gateway_url` | W1-08 | Base URL of the flag-**off** fixture, where authorized routes must return 503. |
| `expected_output_digest` | W1-05 | Normalized output digest the fixture task must still produce after the rejected malformed/oversize commands — the proof the run was unharmed. |
| `oversize_bytes` | W1-05 | Body size for the 413 probe. Default 32768, i.e. twice `MAX_REQUEST_BYTES`. |
| `allowed_parity_fields` | W1-08 | Field-name prefixes allowed to differ between flag-off and flag-on task events. Default `control`, `registration`, `state`. Widen only with a reason recorded in the issue: every added prefix is a difference the parity check stops noticing. |
| `source_digest`, `deployed_digest` | W1-01 (evidence header) | Recorded in the report header. W1-01 fails if the provenance artifact's two digests disagree. |
| `cleanup_items` | cleanup | Exact `(event_id, arrived_at)` pairs to delete. See below. |
| `resource_teardown` | W2-10 | Argv **list** for your fixture's resource-teardown script, which the harness invokes between the security capture and W2-10. Run without a shell, so a single string is refused. Absent → W2-10 is `not_run`. See [`resource_teardown`](#resource_teardown--the-command-the-harness-runs-for-you). |
| `resource_teardown_timeout_seconds` | W2-10 | How long to wait for that script. Default 600. |

### Observation artifacts

Most checks need observations the harness cannot make itself — a `kubectl exec`
from a probe pod, a token that has actually expired, two fixture runs compared, a
jest suite's result. Those are recorded by the operator as small JSON files and
referenced from `artifacts` by path, relative to the config file. The table below
spans both waves; a wave-2 run reads the wave-2 artifacts and does not re-read
wave 1's.

Three outcomes, deliberately distinct:

| Artifact state | Check status | Why |
|---|---|---|
| Not listed / file absent | `not_run` | The harness could not look. Nonzero, never a pass. |
| Present, missing a required key | **`failed`** | A claim without its evidence. Treating it as `not_run` would let an operator satisfy a check by leaving out the awkward field. |
| Present and complete | evaluated | |

Required keys per artifact:

| Artifact | Check | Required keys |
|---|---|---|
| `provenance` | W1-01 | `source_digest`, `deployed_digest`, `ci_jobs`, `isolation_before_listener`, `ordinary_flags_off` |
| `listener_auth` | W1-02 | `missing_token_status`, `wrong_token_status`, `rejected_before_verb_parse` |
| `token_lifecycle` | W1-03 | `before_expiry_status`, `after_expiry_status`, `stale_generation_status`, `ordinary_clock_unchanged` |
| `peer_probe` | W1-04 | `probe_pod`, `gateway_ping_status`, `probe_connect_result`, `policy_selectors`, `timeout_seconds` |
| `fixture_task` | W1-05 | `completed`, `normalized_output_digest` |
| `worker_unavailable` | W1-06 | `state`, `command_acknowledged` |
| `transport_guard` | W1-07 | `blocked_targets`, `redirect_blocked`, `blocked_before_transport` |
| `flag_parity` | W1-08 | `flag_off_events_digest`, `flag_on_events_digest`, `differing_fields`, `ordinary_flags_off` |
| `journal_tests` | W1-09 | `replay_same_id`, `content_conflict`, `bounds_enforced`, `expiry_is_unknown`, `assistant_turns` |
| `negative_tests` | W1-10 | `wrong_account`, `missing_isolation`, `wrong_key`, `absent_required_check`, `unknown_check_id`, `failed_cleanup` |
| `neutral_contract` | W2-02 | `protocol_version`, `adapter_id`, `sdk_version`, `sdk_matches_lockfile`, `adapters`, `second_adapter`, `no_provider_types_in_shared_contract`, `capability_intersection_proven`, `normalized_input_kinds_proven`, `authorization_at_handoff`, `unknown_outcome_supported`, `opaque_attempt_replacement`, `stale_events_rejected`, `disposed_once`, `fresh_private_input_per_attempt`, `session_and_no_option_behavior_preserved`, `cancel_prevents_new_query`, `forced_retry_exercised` |
| `pause_boundary` | W2-03 | `adapter_id`, `sdk_version`, `permission_mode`, `spill_hooks_composed`, `requested`, `held_interval`, `tool_coverage`, `confirmed`, `degraded` |
| `pause_resume` | W2-04 | `released_count`, `session_id_before`, `session_id_after`, `attempt_id_before`, `attempt_id_after`, `interrupt_called`, `initial_prompt_replayed`, `prior_history_preserved`, `task_completed`, `held_tools_admitted_after_resume`, `races` |
| `pause_expiry` | W2-05 | `auto_resumed`, `annotation_count`, `extra_assistant_turn`, `neutral_annotation`, `resolved_before_release`, `pod_killed`, `idle_retry_fired`, `exit_watchdog_fired`, `heartbeats_during_pause`, `paused_distinguishable_from_stalled`, `spill_output_preserved`, `held_hook_timeout`, `deadline_clamp`, `cancellation` |
| `wave2_preflight` | W2-01 | `wave1_evidence`, `merged_revisions`, `protocol_version`, `adapter_id`, `sdk_version`, `package_versions`, `deployed_components`, `ci_gates`, `isolation_before_listener`, `ordinary_flags_off`, `fixture_only_flag_scope`, `fixture_identity`, `creation_ledger` |
| `security_capture` | W2-10 | `captured_before_teardown`, `observed_revisions`, `fixture_identity`, `isolation_present`, `wave1_security`, `unsupported_verbs`, `unsupported_adapter_capabilities`, `general_flag_enablement`, `ordinary_flags_off` — recorded **before** teardown |
| `teardown_verification` | W2-10 | `verified_after_teardown`, `captured_at`, `fixture_identity`, `removals`, `baseline_isolation_present`, `general_flag_enablement`, `ordinary_flags_off` — written **by your `resource_teardown` script**, after the removals |

For `token_lifecycle`, use the expiry produced by the real registration writer
and propagated to the listener as `ADP_CONTROL_TOKEN_EXPIRES_AT`. Record an
authenticated request before that timestamp and repeat it with the same token
afterwards while the fixture is still running: the second request must be 401.
Restarting the listener with a different token proves rotation, not expiry.

Two the harness is strict about, because the evaluation names them:

* **`peer_probe.probe_connect_result`** must be an *actual* connection result
  (`connection refused`, `timed out`), not a restatement of the policy. §7 asks
  for connection results, "not policy YAML alone" — a NetworkPolicy that reads
  correctly and does not apply is exactly the failure this check exists for.
  `timeout_seconds` must be > 0: an unbounded wait cannot distinguish "blocked"
  from "still trying".
* **`journal_tests.assistant_turns`** must be `0`. Polling a read contract must
  cost no model tokens and must not perturb the run.

### `neutral_contract` (W2-02, wave 2)

Record `implemented_verbs: []` for an S3-only build, or
`implemented_verbs: ["pause", "resume"]` once S2 is implemented and proven.
W2-02 compares both live route surfaces with this recorded build contract;
it does not require completed Wave 2 to keep S3's temporary all-false map.
Omitting the field retains the S3 expectation. Abort and steering remain outside
Wave 2. The remaining W2 checks still determine whether the wave is accepted.


The one artifact whose evidence comes from a test run rather than from the
cluster. The neutral contract suite lives where the code does, and the story is
explicit that development and PR tests need no AWS credential — so record its
result here and the harness validates it, like every other artifact.

Produce it from the suite the story names:

```sh
(cd modules/agent-factory/agent && npx jest --runInBand --json --outputFile=/tmp/contract.json \
   --runTestsByPath src/control-runtime.test.ts src/harnesses/claude-control.test.ts \
                    src/utils/resilientQuery.test.ts)
```

Then record, per adapter, whether the neutral suite passed and how many tests
ran. The count is not decoration: a suite that ran **zero** tests exits 0, so
`passed: true` alone is satisfied by a deleted file.

```json
{
  "protocol_version": 1,
  "adapter_id": "claude",
  "sdk_version": "0.3.220",
  "sdk_matches_lockfile": true,
  "adapters": {
    "claude": {"passed": true, "test_count": 61},
    "echo":   {"passed": true, "test_count": 61}
  },
  "second_adapter": {
    "name": "echo",
    "declares_missing_capability": true,
    "imports_provider_sdk": false
  },
  "no_provider_types_in_shared_contract": true,
  "capability_intersection_proven": true,
  "normalized_input_kinds_proven": true,
  "authorization_at_handoff": true,
  "unknown_outcome_supported": true,
  "opaque_attempt_replacement": true,
  "stale_events_rejected": true,
  "disposed_once": true,
  "fresh_private_input_per_attempt": true,
  "session_and_no_option_behavior_preserved": true,
  "cancel_prevents_new_query": true,
  "forced_retry_exercised": true
}
```

Three things the harness is strict about:

* **Both adapters must appear, and the second must not be Claude.** One adapter
  passing a neutral suite proves the suite runs, not that the contract is
  neutral.
* **`second_adapter.declares_missing_capability` must be `true`.** Without a
  capability gap, the intersection is never observed doing anything — an adapter
  that ignored support entirely would pass.
* **`second_adapter.imports_provider_sdk` must be `false`.** A second adapter
  that mimics `Query`/`SDKUserMessage` proves the shared contract accepts
  Claude's shape, which is the opposite of the property being evaluated.

`sdk_version` is compared against the lockfile pin. The streaming-input and
`shouldQuery` behaviours the adapter relies on are *observed* SDK behaviour, not
a documented permanent guarantee, so evidence from another version does not
carry over — a bump is a prompt to rerun this suite.

### `wave2_preflight` (W2-01, wave 2)

Record this **before** you seed any fixture row. W2-01 establishes that the
evidence you are about to gather describes the build under review; if it is
wrong, the other nine checks are correct observations of the wrong thing, which
reads exactly like a passing wave.

Every value below comes from a command you run. Do not carry any of it over from
a previous evaluation — that is precisely the stale-evidence case W2-01 exists to
catch.

```sh
# 1. Wave 1's acceptance, from the accepted run's own result.json — not retyped.
#    `revision` is the commit that run was performed against.
jq '{accepted: true, evaluation, passed, required, cleanup_ok}' \
  "$WAVE1_EVIDENCE_DIR/result.json"

# 2. What is ACTUALLY RUNNING, per component. Record this FIRST: everything
#    below is measured against it. TWO images, TWO workflows — that is the point.
#    `imageID` is the digest the node actually pulled, not the tag it was asked for.
kubectl get pods -n adp-agents -l app=agent-worker \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
kubectl get pods -n adp-gateway -l app=bedrockgateway \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
# The revision each is running, and the revision each was BUILT FROM. These must
# agree: an image built from some other commit is source nobody reviewed. Read
# them from the image's own labels rather than from what you remember deploying.
for img in "$WORKER_DIGEST" "$GATEWAY_DIGEST"; do
  aws ecr batch-get-image --repository-name "${img%%@*}" \
    --image-ids imageDigest="${img##*@}" --query 'images[0].imageManifest' --output text \
    | jq -r '.history[0].v1Compatibility' \
    | jq '.config.Labels | {"org.opencontainers.image.revision"}'
done

# 3. Each story's merge commit. You do NOT record containment — the harness runs
#    `git merge-base --is-ancestor` itself, for every revision against every deployed
#    component, so all you record is the merge commit. Do make sure your checkout has
#    every revision you name, deployed ones included, or W2-01 comes back not_run.
for pr in 3961 3962 3964; do
  gh pr view "$pr" --repo aws-e/adp --json mergeCommit -q .mergeCommit.oid
done
git fetch origin "$WORKER_REVISION" "$GATEWAY_REVISION"   # so the harness can ask

# 3b. The build behind each running image, the digest it pushed, and the digest the
#     registry is serving. THREE responses, archived VERBATIM into
#     deployed_components.<c>.build_record.raw as `build`, `build_log` and `registry`.
#     The harness PARSES each one at its real field locations — buildStatus, the
#     ADP_SOURCE_SHA/IMAGE_TAG overrides, source.location, the `docker push` digest
#     line, imageDetails[0] — so a failed build, a build whose source archive is a
#     different commit, and a document that merely mentions the right strings are all
#     rejected. This is the deployed CodeBuild path (`platform/scripts/codebuild-run.sh`
#     with ADP_RELEASE_BUILD=true), which is the only build path these images ship
#     through.
aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$AWS_REGION" \
  --query 'builds[0].{id:id,projectName:projectName,buildStatus:buildStatus,
           source:source,environment:environment}'
# The digest the build itself says it published — `docker push` prints
# `<tag>: digest: sha256:... size: ...`. This is the ONLY first-hand source for
# built_digest; a value you read off the registry is a different claim.
aws logs get-log-events --region "$AWS_REGION" \
  --log-group-name "/aws/codebuild/${CODEBUILD_PROJECT}" \
  --log-stream-name "${BUILD_ID#*:}" --query 'events[].message' \
  | grep -F 'digest: sha256:'
aws ecr describe-images --repository-name adp-agent-runtime \
  --image-ids imageTag="$IMAGE_TAG" --query 'imageDetails[0]'

# 3c. The required CI gates, BY NAME, and the revision each one tested. A green
#     run of an unrelated commit is true and says nothing about this deployment.
#
#     TWO commands, and the split is not cosmetic. `gh run list` and `gh run view`
#     support DIFFERENT --json field sets: `list` has neither `jobs` nor `attempt`, so
#     asking it for them fails outright with `Unknown JSON field: "jobs"` and you get no
#     response to archive at all. Use `list` only to SELECT a run id from the fields it
#     does support, then `view` that id to retrieve the document you archive — `view` is
#     where `jobs` and `attempt` come from, and the harness requires all three of
#     `jobs`, `attempt` and `event`.
RUN_ID="$(gh run list --repo aws-e/adp --workflow agent-control-ci.yml \
  --commit "$WORKER_REVISION" --limit 1 \
  --json databaseId,headSha,event,status,conclusion,url -q '.[0].databaseId')"
gh run view "$RUN_ID" --repo aws-e/adp \
  --json databaseId,headSha,attempt,event,conclusion,jobs,url
#     ARCHIVE THAT RESPONSE UNMODIFIED, as `raw.run.body`. Do not add fields to it: the
#     harness refuses a run document carrying a `checked_out_revision` key, because
#     `gh run view` does not return one and the revision a run tested must not be a
#     value the operator typed into GitHub's own response.
#
#     If the list returns nothing, the deployed revision was never gated — Agent Control
#     CI triggers on PRs matching its `paths` filters, so a commit outside them has no
#     run of these three jobs. DISPATCH one; do not record another commit's green run:
gh workflow run agent-control-ci.yml --repo aws-e/adp -f source_sha="$WORKER_REVISION"
#     Then take the run id DIRECTLY from the dispatch, not from a --commit filter. A
#     manual run's head is the ref the WORKFLOW FILE was loaded from, not your
#     `source_sha`, so `--commit "$WORKER_REVISION"` will not match the run you just
#     started. Select it by workflow and event instead:
gh run list --repo aws-e/adp --workflow agent-control-ci.yml \
  --event workflow_dispatch --limit 1 \
  --json databaseId,headSha,event,status,conclusion,url
#     Confirm it is the run you dispatched by checking the checkout artifact in 3d: its
#     `checked_out_revision` is your source_sha, which is the fact that identifies it.
#
# 3d. The revision each gate job ACTUALLY checked out, from the job's own artifact.
#     Each of the three test jobs runs `git rev-parse HEAD`, fails if it disagrees with
#     the dispatched SHA, and uploads the answer. That file is written by the job, in the
#     job, about the tree the job had — which is why it is the source for
#     `tested_revision` and `headSha` is not. On a manual run `headSha` names the ref the
#     WORKFLOW FILE was loaded from; the artifact records that separately as
#     `workflow_ref_sha`.
#
#     One artifact per gate, archived as that gate's `raw.checkout.body`. The harness
#     checks its `job` is this gate's job and its `run_id`/`run_attempt` are the run and
#     attempt the archived run document describes, so an artifact from another job, run
#     or attempt is refused rather than accepted as this gate's checkout. `attempt` and
#     `event` are REQUIRED in the run document, because a missing one would skip that
#     binding rather than fail it.
#
#     If the gate you are archiving is a `pull_request` run rather than a dispatch, the
#     checked-out SHA will NOT equal `headSha`, and that is correct: GitHub builds a
#     temporary MERGE of the branch into its base, and that merge commit is the tree the
#     tests ran in. `headSha` is the branch tip, which was never built. Archive both
#     documents as they are — the harness requires the checkout to CONTAIN the head
#     (which a merge of it does) rather than to equal it, so the honest pair is accepted
#     and nothing has to be reconciled by hand. Do not "fix" the disagreement.
for JOB in agent-control-tests worker-control-tests control-evaluation-harness-tests; do
  gh run download "$RUN_ID" --repo aws-e/adp \
    -n "checked-out-revision-${JOB}" -D "/tmp/gate/${JOB}"
  cat "/tmp/gate/${JOB}/${JOB}.json"   # archive verbatim as raw.checkout.body
done

# 4. Package versions, from the lockfile that actually shipped in the image.
kubectl exec -n adp-agents deploy/agent-worker -- \
  node -e 'const p=require("/app/package.json");console.log(JSON.stringify(p.dependencies))'

# 5. The flag's scope. The SECOND command is the one that matters.
kubectl get cm -n adp-agents control-flags -o jsonpath='{.data}'
# Every other environment, enumerated — the answer must be an empty list.
for env in dev staging prod embark1; do
  aws ssm get-parameter --name "/adp/$env/control/enabled" \
    --query 'Parameter.Value' --output text 2>/dev/null
done

# 6. Isolation BEFORE the listener started. Ordering, not presence.
kubectl get networkpolicy -n adp-agents -o json | jq '.items[].metadata.creationTimestamp'
kubectl get pods -n adp-agents -l app=agent-worker \
  -o jsonpath='{.items[0].status.startTime}'

# 7. The creation ledger: every resource this fixture creates, with the identity
#    it was observed to have AT CREATION. Record each one as you create it — this
#    is what teardown completeness is later measured against, and a resource you
#    forget to list here is one nobody will look for afterwards.
#
#    Identities, not names. `kubectl apply` ADOPTS a pre-existing object of the
#    same name, so a name establishes neither that you created the thing nor,
#    later, that the thing removed was the thing you created. Check `created`
#    honestly: an adopted resource is not yours to delete.
kubectl get deploy,pod,networkpolicy -n adp-agents -l control-fixture=true \
  -o jsonpath='{range .items[*]}{.kind}/{.metadata.name} uid:{.metadata.uid}{"\n"}{end}'
```

Then write the file. `enabled_elsewhere` is a **list**, not a boolean: the claim
is "nowhere else", and an enumerated empty list is the only form of it anyone can
audit. `fixture_environment` must equal the config's `environment`.

```json
{
  "wave1_evidence": {
    "accepted": true, "evaluation": "3967", "passed": 10, "required": 10,
    "cleanup_ok": true,
    "revision": "1d0e3ab6a8c24f5b9e7d0c1a2b3f4e5d6c7b8a90",
    "run_id": "eval-3967-run-1"
  },
  "merged_revisions": {
    "S2": {"merged": true, "revision": "<40-char SHA>"},
    "S3": {"merged": true, "revision": "<40-char SHA>"},
    "S5": {"merged": true, "revision": "<40-char SHA>"}
  },
  "protocol_version": 1,
  "adapter_id": "claude",
  "sdk_version": "0.3.220",
  "package_versions": {
    "@anthropic-ai/claude-agent-sdk": "0.3.220",
    "control-runtime": "1.0.0"
  },
  "deployed_components": {
    "worker": {
      "revision": "<40-char SHA — what this component is RUNNING>",
      "source_revision": "<40-char SHA — what its image was BUILT FROM>",
      "image_digest": "sha256:<64 hex>",
      "build_record": {
        "project": "adp-dev-agent-runtime",
        "build_id": "adp-dev-agent-runtime:9f8e7d6c-1234-4abc-8def-0123456789ab",
        "build_url": "https://us-east-1.console.aws.amazon.com/codesuite/codebuild/projects/adp-dev-agent-runtime/build/...",
        "built_revision": "<40-char SHA — the build's ADP_SOURCE_SHA>",
        "image_tag": "<the build's IMAGE_TAG>",
        "built_digest": "sha256:<64 hex — from the build log's `docker push` line>",
        "repository": "adp-agent-runtime",
        "registry_digest": "sha256:<64 hex — as the REGISTRY reports it>",
        "raw": {
          "build": {
            "command": "aws codebuild batch-get-builds --ids <build-id>",
            "retrieved_at": "<ISO-8601>",
            "body": {"...": "the response, verbatim — buildStatus must be SUCCEEDED"}
          },
          "build_log": {
            "command": "aws logs get-log-events --log-group-name /aws/codebuild/<project> --log-stream-name <id>",
            "retrieved_at": "<ISO-8601>",
            "body": {"...": "the response, verbatim — must contain `<tag>: digest: sha256:... size: ...`"}
          },
          "registry": {
            "command": "aws ecr describe-images --repository-name adp-agent-runtime --image-ids imageTag=<tag>",
            "retrieved_at": "<ISO-8601>",
            "body": {"...": "the response, verbatim"}
          }
        }
      }
    },
    "gateway": {
      "revision": "<40-char SHA>",
      "source_revision": "<40-char SHA>",
      "image_digest": "sha256:<64 hex — MUST differ from the worker's>",
      "build_record": {"...": "as above, from its own CodeBuild project and ECR repo"}
    }
  },
  "ci_gates": {
    "Agent control tests": {
      "status": "passed",
      "run_id": "18234567890",
      "run_url": "https://github.com/aws-e/adp/actions/runs/18234567890",
      "tested_revision": "<40-char SHA>",
      "raw": {
        "run": {
          "command": "gh run view 18234567890 --json databaseId,headSha,attempt,event,conclusion,jobs",
          "retrieved_at": "<ISO-8601>",
          "body": {"...": "the response VERBATIM and unmodified — the named job's conclusion must be `success`. Adding a `checked_out_revision` key here is refused"}
        },
        "checkout": {
          "command": "gh run download 18234567890 -n checked-out-revision-agent-control-tests",
          "retrieved_at": "<ISO-8601>",
          "body": {"...": "the artifact the job uploaded, verbatim: job, checked_out_revision, run_id, run_attempt, event_name, workflow_ref_sha. `tested_revision` is read from here"}
        }
      }
    },
    "Worker control tests": {"...": "as above"},
    "Control evaluation harness tests": {"...": "as above"}
  },
  "isolation_before_listener": true,
  "ordinary_flags_off": true,
  "fixture_only_flag_scope": {
    "enabled_in_fixture": true,
    "enabled_elsewhere": [],
    "fixture_environment": "dev-control-fixture-embark1"
  },
  "fixture_identity": {
    "account_id": "879318057152",
    "environment": "dev-control-fixture-embark1",
    "run_id": "<the config's live_run_id>"
  },
  "creation_ledger": [
    {"kind": "Deployment", "name": "agent-worker-fixture-1",
     "identity": "uid:<the UID you observed at creation>", "created": true},
    {"kind": "Pod", "name": "control-probe-1",
     "identity": "uid:<...>", "created": true},
    {"kind": "NetworkPolicy", "name": "control-fixture-isolation",
     "identity": "uid:<...>", "created": true}
  ]
}
```

Six things the harness is strict about, each because a plausible recording would
otherwise pass:

* **Containment is computed by the harness, not recorded by you.** A correct
  deployment is *newer* than the commits it contains — it carries the story plus
  whatever landed after it — so the relation is containment, not equality. But you do
  not record it: the harness runs `git merge-base --is-ancestor` itself, in the
  checkout you run it from, for every (revision × deployed component) pair. There is
  no `contained_in` field any more, and writing one changes nothing. It used to be
  read, which was circular — `is_ancestor: true` is the conclusion the check exists to
  reach.

  Two consequences for you. **Run the harness in a full clone that contains every
  revision you name**, including both deployed component revisions; a shallow clone
  or an unfetched commit makes W2-01 `not_run` with the missing revision named. And
  **an invented SHA now fails here**, because git has never heard of it — which is the
  point.
* **Wave 1's `run_id`, not just its summary.** The counts are recorded separately
  from `accepted` because an acceptance at 9/10 is either a different run or a
  misremembered one, and `accepted: true` cannot tell those apart. The run ID is
  what lets a reviewer retrieve wave 1's report instead of trusting this summary
  of it. A bare `compatible_with_current_revision: true` is **not** accepted —
  an asserted compatibility claim is not evidence of compatibility.
* **A `build_record` per component, with the archived tool output PARSED behind it.**
  This is the link that makes a digest mean anything, and a pair of agreeing fields is
  not it: `source_revision == revision` only establishes that you typed one SHA twice,
  and a digest that parses is not a digest anyone published.

  **This is the deployed CodeBuild path, specifically** — `platform/scripts/codebuild-run.sh`
  with `ADP_RELEASE_BUILD=true`, which is how these images actually ship. An earlier
  version of this contract modelled a GitHub Actions build; a record describing a build
  path we do not deploy through cannot establish anything about a running image. Three
  commands produce everything, and all three responses go into `raw` verbatim:

  ```bash
  # 1. the build: which one, whether it SUCCEEDED, what source it consumed
  aws codebuild batch-get-builds --ids <build-id> --region us-east-1 \
    --query 'builds[0].{id:id,projectName:projectName,buildStatus:buildStatus,
             source:source,environment:environment}'
  # 2. the build log: the digest the build's own `docker push` reported for its tag
  aws logs get-log-events --region us-east-1 \
    --log-group-name /aws/codebuild/<project> --log-stream-name <build-id after the colon>
  # 3. the registry: what that tag serves now
  aws ecr describe-images --repository-name <repo> --image-ids imageTag=<tag> \
    --query 'imageDetails[0]'
  ```

  The harness **parses** each body at its real field locations and rejects on the
  values, not on what the text happens to contain:

  | What it reads | Where | What it refuses |
  |---|---|---|
  | `builds[0].buildStatus` | the build | anything but `SUCCEEDED` — FAILED, FAULT, STOPPED, TIMED_OUT, IN_PROGRESS published no image |
  | `builds[0].id` | the build | a summary `build_id` that disagrees with it |
  | `ADP_SOURCE_SHA`, `IMAGE_TAG` | `environment.environmentVariables` | a summary `built_revision` / `image_tag` that disagrees |
  | `builds[0].source.location` | the build | a source archive key naming a *different* revision than `ADP_SOURCE_SHA` |
  | `<tag>: digest: sha256:… size: …` | the build log | a `built_digest` the build never said it pushed, or no push line at all |
  | `imageDetails[0].imageDigest` / `imageTags` | the registry | a served digest that disagrees, or an image not carrying the pushed tag |
  | `jobs[].conclusion` for the named gate | the CI run | a job that failed, was cancelled or skipped, is unfinished, or is absent from the run |
  | `checked_out_revision` | the gate job's uploaded artifact | a SHA hand-added to the run response, a missing artifact, or one from another job, run or attempt |
  | `headSha` vs the checkout | the run document and the artifact, per trigger | a `pull_request` checkout that does not *contain* the head; a `workflow_dispatch` or `pull_request` `workflow_ref_sha` that disagrees with what it should equal; a missing `attempt` or `event` |

  Why it is written this way: the previous version dumped each body to JSON text and
  asked whether the expected strings appeared *anywhere* in it. Root's review
  demonstrated what that accepts — a build whose own outcome was `failure`, a CI run
  whose every job had failed, and a build document replaced entirely by
  `{"unrelated_notes": "<build id> … <revision>"}`. All three passed, because a red
  document and an unrelated note both contain the right words. And `built_digest` was
  never compared against build output at all, only against fields the same operator
  wrote. Each of those three is now a regression test.

  Paste each response verbatim into `raw.<name>.body`, with the command that produced
  it and the time you ran it. Do not summarize the body — the body is the evidence, and
  the fields above it are the summary the harness checks against it. Set
  `PUBLISH_LATEST=false` and `IMAGE_TAG` to the full SHA so no moving tag sits between
  the commit and the digest.
* **The required CI gates by name, on a deployed revision, with their run document.**
  The names are the ones `.github/workflows/agent-control-ci.yml` defines. A nonempty
  map of green results is not enough — `{"anything": "passed"}` demonstrates your
  spelling, not the build's gates — so each required name must be present, name a
  `tested_revision` that is one of the deployed ones, and carry `run_id`, `run_url`
  and the archived `raw.run` document. A green gate on an unrelated commit is true and
  irrelevant; an arbitrary truthy `run_id` (it used to be accepted, including literal
  `true`) identifies nothing.

  The run document is parsed, not searched: `databaseId` must equal the recorded
  `run_id`, and **the named job must itself have concluded `success`**. A run's own
  green status does not carry a job that was skipped, and a `null` conclusion is an
  unfinished job rather than a passing one. `gh run view <id> --json
  databaseId,headSha,attempt,event,conclusion,jobs` gives you all of it.

  **Archive that response unmodified, and take the tested revision from the job's own
  artifact.** `agent-control-ci.yml` accepts a `workflow_dispatch` with a required
  `source_sha`, which is how you gate a revision the PR-filtered triggers never ran.
  Each of its three test jobs then runs `git rev-parse HEAD`, fails if that disagrees
  with the dispatched SHA, and uploads the answer as `checked-out-revision-<job>`.
  Archive that artifact as the gate's second raw document, `raw.checkout`, and download
  one per gate.

  `tested_revision` is read from **there**, never from `headSha` — on a manual run
  `headSha` names the ref the *workflow file* was loaded from, and the artifact records
  that separately as `workflow_ref_sha`. An earlier revision of this runbook told you
  to paste `checked_out_revision` into the run response instead; that is now refused,
  because it made the one fact the manual path exists to establish a value you typed
  into a document GitHub otherwise wrote. The artifact's `job` must be this gate's job
  and its `run_id`/`run_attempt` must match the archived run, so an artifact from
  another job, another run or an earlier attempt is refused rather than accepted — each
  job checks out independently, and a re-run checks out afresh. The run document must
  carry `attempt` and `event`: without them those bindings would be skipped rather than
  checked, so a less complete archive would be held to a weaker standard.

  **How `headSha` relates to the checkout depends on the trigger, and the harness judges
  the three cases differently.** On a `pull_request` run GitHub builds a temporary *merge*
  of the branch into its base, so the job checks out a commit that is deliberately not the
  branch tip — the tip was never built. The harness therefore requires the checkout to
  **contain** the head, which a merge of it does, rather than to equal it. Archive both
  documents exactly as you got them; the disagreement is expected and is not something to
  reconcile. On a `workflow_dispatch` run `headSha` names the ref the *workflow file* was
  loaded from, which the artifact records separately as `workflow_ref_sha`, and the two
  must agree. On a `pull_request` run `GITHUB_SHA` is the merge commit, so
  `workflow_ref_sha` must equal the checked-out revision there. Every other trigger checks
  out the head directly, where equality is the correct relation.

  An earlier revision demanded equality for everything that was not a manual dispatch,
  which rejected a real `pull_request` gate whose documents were both authentic and
  unmodified. That is a regression test now, built from the real artifact of an actual
  run: a rule that refuses genuine evidence pushes the next operator into making the
  documents agree by hand, which is the failure all of this exists to prevent.
* **The creation ledger, with observed identities.** Teardown completeness is
  measured against this list, in both directions. A resource you omit is one
  nobody looks for afterwards, and a removal of something absent from the ledger
  means teardown had an unrelated resource in reach.
* **Full 40-character SHAs and well-formed `sha256:` digests.** A branch name or
  short SHA names whatever a ref happened to point at. Two equally-malformed
  digests compare *equal*, so shape is validated before comparison. The two
  deployed digests must also differ from each other: separate images from
  separate Dockerfiles, so an equal pair means one value was copied over the other
  — which would make a stale half-deployment pass every per-component check.

`cleanup_items` must already cover every row this wave seeds (`live_run_id`,
`terminal_run_id`, `aborted_run_id`) with **both** key halves. W2-01 fails on an
undeclared teardown, deliberately before the fixture exists: cleanup is bounded
to declared pairs and there is no scan to fall back on. `unknown_run_id` must
**not** appear — it names a row that must not exist, so "cleaning" it would mean
deleting something the harness never created.

### W2-10's two artifacts, and why there are two

W2-10 reads `security_capture` and `teardown_verification`. They were one file,
and splitting them is the ordering correction described below — the single most
important thing to get right in this runbook.

**Capture, then tear down, then verify.** A security posture is a property of a
*running* deployment: you can only observe it while the fixture exists. Absence is
the opposite: it can only be observed once the fixture is gone. One artifact
recorded at one instant cannot hold both, and the version that tried made W2-10
ask a deleted run to answer a request — so a **correct** teardown produced a
not-found and the check reported `not_run`. The wave could not pass by doing the
right thing.

So the order is: run the nine checks → **record `security_capture` while
everything is still up** → the harness runs your `resource_teardown` script, which
removes the resources and **writes `teardown_verification`** → the harness deletes the
rows → W2-10 is evaluated. You are responsible for the capture and for the teardown
script; the harness owns the sequencing, and it refuses an absence artifact that did not
change across the teardown it invoked. Never fill in the capture from memory afterwards:
if you cannot make it, W2-10 is `not_run` (nonzero) and you rerun the evaluation, which
is not something a later observation can recover.

Neither file carries the row deletions. W2-10 verifies those against the
harness's own DeleteItem and consistent-read record, so there is nothing you can
write in either one that reports a successful teardown — a `cleanup_ok: true`
field is simply ignored. And a not-found is never accepted *as* the pass: it is
equally consistent with a correct teardown and with a fixture that never existed.

### `security_capture` (W2-10, wave 2) — BEFORE teardown

Record this while the fixture is still running, after the other nine checks have
completed. A non-200 on any read here is a real defect: the deployment is up, so
it should answer.

```sh
# The build these observations are ABOUT, per component. Re-read it now rather
# than copying the preflight: if it changed mid-evaluation you need to know.
# This is NOT any story's merge commit — it is what is running.
kubectl get pods -n adp-agents -l app=agent-worker \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
kubectl get pods -n adp-gateway -l app=bedrockgateway \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'

# Isolation, while the listener is still up — the window DP-INV-1 is about.
kubectl get networkpolicy -n adp-agents control-fixture-isolation

# The flag must not have widened during the evaluation.
kubectl get cm -n adp-agents control-flags -o jsonpath='{.data}'

# Unsupported verbs must refuse. Run these as the OWNER session: an
# unauthenticated 401 proves nothing about whether the verb is implemented.
# Only verbs the capability map reports FALSE. Do not POST pause or resume —
# they are implemented in this wave, so they would act on a live run the other
# checks are still describing. An unimplemented verb's handler refuses before
# doing anything, which is what makes this safe to run against the fixture.
for verb in steer abort; do
  curl -sS -o /dev/null -w "$verb=%{http_code}\n" -X POST \
    -H "Authorization: Bearer $CONTROL_EVAL_OWNER_SESSION" \
    -H 'Content-Type: application/json' \
    -d '{"command_id":"'"$(uuidgen)"'"}' \
    "$GATEWAY_URL/activity/invocations/$LIVE_RUN_ID/agent/$verb"
done

# Wave 1's security properties, re-observed on the current revision. Rerun the
# wave-1 probes rather than reusing their results: that is the point.
```

```json
{
  "captured_before_teardown": true,
  "observed_revisions": {
    "worker": {"revision": "<40-char SHA>", "image_digest": "sha256:<64 hex>"},
    "gateway": {"revision": "<40-char SHA>", "image_digest": "sha256:<64 hex>"}
  },
  "fixture_identity": {
    "account_id": "879318057152",
    "environment": "dev-control-fixture-embark1",
    "run_id": "<the config's live_run_id>"
  },
  "isolation_present": true,
  "wave1_security": {
    "unauthenticated_rejected": true,
    "cross_tenant_indistinguishable": true,
    "nonowner_indistinguishable": true,
    "transport_targets_blocked": true,
    "no_token_in_public_state": true,
    "admission_authorization_preserved": true,
    "delivery_authorization_preserved": true
  },
  "unsupported_verbs": {"steer": 501, "abort": 501},
  "unsupported_adapter_capabilities": {"steer": false, "abort": false},
  "general_flag_enablement": false,
  "ordinary_flags_off": true
}
```

What the harness is strict about here:

* **`observed_revisions` must match the preflight's `deployed_components`.** Per
  component, revision *and* digest. These are two independently recorded operator
  observations of the same thing, so a disagreement means one of them describes a
  different build. Note what it is **not** compared against: a story's merge
  commit. An observation bound to S2's merge commit would be evidence about a
  build that is not deployed.
* **`captured_before_teardown` must be `true`.** Recorded after teardown, these
  describe an environment that no longer existed.
* **All seven `wave1_security` properties must be present.** An unrecorded
  property is one nobody re-observed. `admission_authorization_preserved` and
  `delivery_authorization_preserved` are worth naming: #5029 requires
  authorization to be revalidated immediately before physical handoff, and
  pause/resume is precisely the code path that could have moved that revalidation
  earlier.
* **`general_flag_enablement` must be `false`.** Widening the flag to make a check
  pass is the specific shortcut this forbids.
* **The harness makes its own attempt too.** It POSTs each verb your capability map
  reports `false` as an authorized owner and compares the status against your
  `unsupported_verbs`. A capability map is the deployment's claim about itself; the
  status returned when the verb is actually POSTed is what a caller gets, and an
  enabled-but-still-advertised-false verb is only visible to the attempt. If the
  two disagree, W2-10 fails — the deployment is what the next operator inherits.

### `resource_teardown` — the command the harness runs for you

You do not write `teardown_verification` by hand, and you must not write it before
teardown. It is produced **by your teardown script**, which the harness invokes
itself.

Declare the script in the fixture config as an argv list:

```json
"resource_teardown": ["/opt/adp/fixtures/teardown-control-fixture.sh", "--env", "dev-control-fixture-embark1"],
"resource_teardown_timeout_seconds": 600
```

The harness runs it **without a shell** (so there is no pipeline, no globbing and no
`&&`), between recording `security_capture` and evaluating W2-10, and before the row
cleanup. It records the exit code, the start and finish times, and a SHA-256 digest of
the output — a digest rather than the text, because command output is a plausible place
for a token or an ARN to surface and this record goes into the evidence file.

Your script has to do three things, in this order:

1. **Remove every resource in the preflight's creation ledger.** Workloads first, the
   NetworkPolicies last (see below).
2. **Read back their absence, by UID.**
3. **Write `teardown_verification` to the path the config's `artifacts` mapping
   declares for it.**

If it is not declared, W2-10 is `not_run` — not a pass. If it exits nonzero, W2-10
fails. Both are nonzero overall, and neither is recoverable by writing the artifact
yourself, which is the point:

> **Why the harness runs this instead of trusting you to.** An artifact cannot
> establish its own freshness. The earlier version of this evaluation captured live
> state, deleted rows, and then read a file that already said the pods and queues were
> gone — so no sequential run could have produced that file honestly at that moment,
> and the only way to have it was to write it beforehand. The harness now digests the
> artifact immediately before invoking your script and compares it against what it
> reads afterwards. **A file whose bytes did not change across the teardown is refused
> as prefilled**, no matter what timestamp is inside it.

The harness does not delete resources itself and will not be given permission to. A
read-only evaluator that could delete cluster workloads is a much larger blast radius
than the thing it verifies, and the deletion logic belongs with the scripts that
created the resources. What the harness contributes is the **ordering** and the
first-hand record that the step ran where it claims to have run.

### `teardown_verification` (W2-10, wave 2) — written BY your teardown script

Only absence, because absence is the only thing teardown produces. Nothing here
requires a removed resource to respond.

```sh
# Inside your teardown script, AFTER the deletions and BEFORE it exits.
#
# Every resource in the preflight's creation ledger, by IDENTITY. Absence is
# established by a read; record the command as `observed_by`, because an
# unattributed `true` is a claim, not an observation.
#
# Compare UIDs, not names: a same-named object recreated after teardown also
# satisfies "no object called control-probe-1 exists"? No — it satisfies the
# opposite, and that is the trap. The UID is what distinguishes "this exact
# object is gone" from "nothing by that name is here right now".
kubectl get deploy agent-worker-fixture-1 -n adp-agents \
  -o jsonpath='{.metadata.uid}' --ignore-not-found
kubectl get pod control-probe-1 -n adp-agents \
  -o jsonpath='{.metadata.uid}' --ignore-not-found

# The fixture's OWN policy must be gone — it is a ledger resource. Remove it LAST,
# after the workloads above, and record when each removal happened.
kubectl get networkpolicy -n adp-agents control-fixture-isolation --ignore-not-found

# The environment's BASELINE isolation must still be there. Different object,
# opposite lifecycle: it belongs to the namespace, not to this run.
kubectl get networkpolicy -n adp-agents adp-agents-baseline-isolation

# And the flag must not have been left enabled.
kubectl get cm -n adp-agents control-flags -o jsonpath='{.data}'
```

```json
{
  "verified_after_teardown": true,
  "captured_at": "2026-09-24T14:32:07Z",
  "fixture_identity": {
    "account_id": "879318057152",
    "environment": "dev-control-fixture-embark1",
    "run_id": "<the SAME live_run_id as the capture>"
  },
  "removals": [
    {"identity": "uid:<the ledger's Deployment UID>", "absent": true,
     "observed_by": "kubectl get deploy agent-worker-fixture-1 --ignore-not-found",
     "removed_at": "2026-09-24T14:31:40Z"},
    {"identity": "uid:<the ledger's Pod UID>", "absent": true,
     "observed_by": "kubectl get pod control-probe-1 --ignore-not-found",
     "removed_at": "2026-09-24T14:31:45Z"},
    {"identity": "uid:<the ledger's NetworkPolicy UID>", "absent": true,
     "observed_by": "kubectl get networkpolicy control-fixture-isolation --ignore-not-found",
     "removed_at": "2026-09-24T14:32:02Z"}
  ],
  "baseline_isolation_present": true,
  "general_flag_enablement": false,
  "ordinary_flags_off": true
}
```

What the harness is strict about here:

* **`removals` reconciles against the creation ledger, both ways.** Every ledger
  identity needs an absence observation — a resource nobody looked for is how a
  control-enabled workload outlives its evaluation — and every removal must name a
  ledger identity, because removing something this fixture did not create means
  teardown had an unrelated resource in reach. This replaced a caller-keyed map of
  names to booleans, under which **omitting** a leaked resource passed.
* **Identities, not names.** `kubectl apply` adopts a pre-existing same-name
  object, so a name proves neither ownership nor that the thing removed was the
  thing created.
* **`observed_by` on every entry.** Absence has to be established by an actual
  read. Present-but-empty is rejected along with absent.
* **`removed_at` on every entry, and the workload must die before its policy.**
  Both are ledger resources, so both are gone at the end — but deleting the
  NetworkPolicy *first* leaves an interval in which a control-enabled pod is running
  with its ingress restriction already removed. That window is worse than either end
  state, and it is invisible to a check that only looks at what is true afterwards.
  DP-INV-1 is about the interval, so the timestamps are compared, not merely
  collected.
* **`baseline_isolation_present`, not the fixture's own policy.** These are two
  different objects with opposite lifecycles, and conflating them made this check
  unsatisfiable: the fixture's NetworkPolicies are ledger resources, so a single
  "isolation is present" requirement meant either leaving a policy behind (a leak) or
  reporting isolation gone. Your fixture's policies must be **removed**; the
  environment's persistent baseline isolation must **survive**.
* **`captured_at` must postdate the teardown's start.** Your script removes, reads,
  then writes — so a correct `captured_at` falls inside the window the harness
  recorded. A value predating it describes the fixture while it still existed.
* **`verified_after_teardown` must be `true`, and the run must match the
  capture's.** A capture of fixture A with a teardown of fixture B is two
  individually consistent files describing a posture that was never torn down and
  a teardown whose posture was never observed.

## Wave 2 is fully implemented; completing it is still on you

`--wave 2` registers all ten checks from evaluation #3968, and as of #5825 every
one of them has a predicate: S3 provides W2-02, S2 provides W2-03 through W2-05,
S5 provides W2-06 through W2-09, and #5825 provides W2-01 (the consolidated
preflight) and W2-10 (verified cleanup and the security recheck).

What changed is *why* the command can exit nonzero. It used to be nonzero because
two checks had no implementation, which no operator could fix. Now a nonzero
wave-2 run means one of three things, and all three are yours to act on:

* a required artifact is missing, so the check is `not_run` — the message names
  the file and its required keys;
* the observed deployment disagrees with the contract, so a check `failed`;
* cleanup did not complete, so W2-10 `failed` and `cleanup_ok` is `false`.

A complete wave-2 report is therefore now reachable — ten passed, `cleanup_ok:
true`, exit 0. Reaching it in a real environment is what evaluation #3968 needs;
the harness being able to report it is not itself that evidence.

One correction is worth knowing about if you ran an earlier revision of this
harness: W2-10 used to make its live capability reads *after* cleanup, against
the run cleanup had just deleted. A correct teardown therefore produced a
not-found and W2-10 reported `not_run` — the wave could not pass by doing the
right thing, and the only way to make it green was to leave a row behind. The
reads now happen before teardown (`security_capture`) and only absence is
verified afterwards (`teardown_verification`). If you have a `not_run` on W2-10
from an older run, that is the likely cause and it was never your fixture's fault.

## Cleanup

Always runs, including on the failure path — that is when a fixture is most
likely to be left with a live listener. Raw evidence is written first, and the
pre-teardown security capture is taken before any deletion, so a failure in it
cannot become the reason the fixture stays up.

Cleanup is bounded to the exact `(event_id, arrived_at)` pairs in
`cleanup_items`. There is no scan, no query, no prefix and no wildcard, so an
item the harness was not told about is unreachable by construction rather than
by care. A pair missing either half is refused rather than guessed at, because a
delete keyed on `event_id` alone could match an unrelated item. A malformed entry
is recorded as a refusal rather than allowed to abort the remaining deletions.
Absence is confirmed with a consistent read, since an eventually-consistent one
can report an item gone before it is.

A failure of the harness's own row deletions is exit 5, which fails the gate even
with ten passing checks. **Never** purge a shared queue or delete ordinary
objects to make cleanup succeed.

`cleanup_ok` covers the **whole** fixture, not just the rows: the rows were
deleted, *and* the fixture's `resource_teardown` ran and succeeded, *and* W2-10
verified the resources absent. Any of the three unestablished makes it `false`,
with `fixture_cleanup` carrying which. That distinction matters when you are
reading a nonzero run to decide whether the environment is safe to reuse — a
teardown script that cannot be executed leaves your pods running, and this field
is what says so.

Fixture workloads and probe pods are yours to remove — the harness deletes only
the synthetic rows it was given. What it *does* do is check your removals against
the preflight's `creation_ledger`, so inventory the fixture as you build it: a
resource missing from that ledger is one W2-10 cannot ask you about.

## Evidence

`$CONTROL_EVIDENCE_DIR/result.json`:

```json
{
  "wave": 1, "evaluation": "3967", "supported_verbs": [],
  "checks": {
    "W1-01": {"status": "passed", "acceptance_ids": ["Gate/regression"],
              "description": "...", "evidence": [{"command": "...", "status": 200}],
              "message": ""}
  },
  "required": 10, "passed": 10, "failed": 0, "skipped": 0, "not_run": 0,
  "cleanup_ok": true,
  "fixture_cleanup": {
    "ok": true, "rows_ok": true, "resources_ok": true,
    "absence_verified": true, "notes": []
  },
  "cleanup_notes": ["removed msg-0000000000000001/2026-09-12T10:00:00Z; consistent read confirms absence"],
  "cleanup": {
    "ok": true, "declared_items": 3,
    "deletions": [
      {"event_id": "msg-0000000000000001", "arrived_at": "2026-09-12T10:00:00Z",
       "both_keys_present": true, "deleted": true, "confirmed_absent": true,
       "error": null}
    ]
  }
}
```

`checks` is an **object keyed by check ID**, which is what makes
`.checks[$id]` in the `check()` helper work. Every entry carries a nonempty
`evidence` list; a status string without evidence is not sufficient.

`cleanup` is the harness's first-hand **row** teardown record and it is what
W2-10's deletion half is verified against, so it is worth reading directly rather
than trusting W2-10's verdict. It is deliberately narrower than `cleanup_ok`:
`cleanup.ok` says the declared rows are gone and nothing more, so read
`fixture_cleanup` for whether the fixture as a whole was disposed of.
One entry per declared row, each stating that both key halves were used,
that a delete was issued, and that a **consistent** read confirmed the row gone.
`both_keys_present: false` means the harness refused a partial-key delete rather
than guessing at the missing half. A row present in `deletions` that is not in
your `cleanup_items` would mean an undeclared deletion — there is no code path
that produces one, and W2-10 fails if it ever sees one.

Credentials never reach this file, by two independent mechanisms:

1. Recorded commands are assembled with a `Bearer $<role>` placeholder, so a
   token is never in the structure — not even transiently.
2. Redaction scrubs secret-shaped keys and secret-shaped *values* (bearer
   headers, `AKIA`/`ASIA` keys, `gh*_` tokens, JWTs) on the way out, as a second
   line of defence for anything a future field forgets.

Pod addresses and ports are likewise absent: they are internal fields of the
control record and are never part of a response model.

Attach one complete run to #3967. Fix failures through the owning story's PR
(reopen it, or link a focused defect if it merged), deploy, then **rerun the
whole wave** — a partial rerun is not evidence that the wave passes.

## Check IDs

The ten IDs and their meanings come from #3967's acceptance table, which is
authoritative. They are pinned by test
(`TestCheckIdsMatchTheEvaluationFile`) because the failure mode is quiet: a
harness keyed by the same IDs with a different partition of the space satisfies
the `jq` gate and every `check()` call while proving something other than what
the evaluation requires, and nothing downstream flags it.

| ID | Acceptance IDs | Subject |
|---|---|---|
| W1-01 | Gate/regression | Preflight: account, four identities, exact row key + generation, digests, CI jobs, isolation before listener start, ordinary flags off |
| W1-02 | AC-S1, AC-S2 | Both adapters × four verbs: 401 unauthenticated; identical 404 for wrong tenant / nonowner / unknown; 401 before verb parsing; authorized verbs 501; all capabilities false |
| W1-03 | AC-S3 | Token works before expiry, fails after; stale generation fails; ordinary run clocks unchanged; no token in public state |
| W1-04 | AC-S4 | Gateway ping reaches the fixture worker; a named non-gateway probe pod cannot, within a finite timeout |
| W1-05 | AC-S5 | 400 malformed, 400 on extra `actor`/`target`/`token`, 413 oversize; fixture task still completes with unchanged output |
| W1-06 | AC-S6 | Owner 410 on a terminal row, others still 404; dead worker reports unavailable and cannot acknowledge; teardown cleared private fields |
| W1-07 | AC-S7 | Unregistered IP, wrong port, metadata, link-local, loopback, public and redirects blocked before transport; no address or token in responses |
| W1-08 | AC-F1, AC-F2 | Flag-off vs flag-on parity on normalized events; authorized flag-off routes 503; ordinary flags off |
| W1-09 | Gate/regression | Live ping/state matches `control_schemas.py` field for field; zero assistant turns; journal replay, conflict, bounds, expiry-as-unknown |
| W1-10 | Gate/regression | The harness's own negative tests: wrong account, missing isolation, wrong key, absent and unknown required check, failed cleanup |

### Wave 2 (evaluation #3968)

Wave 2's manifest is registered in full — all ten IDs, from #3968's acceptance
table — and every one is implemented. S3 (#3962) implements W2-02, S2 (#3961)
implements W2-03 through W2-05, S5 (#3964) implements W2-06 through W2-09, and
#5825 implements W2-01 and W2-10.

| ID | Acceptance IDs | Subject | Owner |
|---|---|---|---|
| W2-01 | Gate/regression | Wave-2 preflight consolidation | **#5825** (defect on #3968) |
| W2-02 | AC-T7 | Neutral adapter contract suite | **S3 #3962** |
| W2-03 | AC-P1 | Pause admission control | S2 #3961 |
| W2-04 | AC-P2 | Resume semantics | S2 #3961 |
| W2-05 | AC-P3, AC-P5, AC-P6 | Auto-resume and watchdog behaviour | S2 #3961 |
| W2-06 | AC-A3, AC-A9 | Aborted row is terminal with a completion time, filterable, and reached that state via ADP finalization rather than a native interrupt | **S5** |
| W2-07 | AC-A10 | Each aborted row counted exactly once; no other outcome reclassified | **S5** |
| W2-08 | AC-A11, AC-A12 | Writer allowlist and reader vocabulary in parity across both deployed images; unknown statuses rejected before the write | **S5** |
| W2-09 | AC-A10 | The live run-stats response carries the aborted counter at every level | **S5** |
| W2-10 | Gate/regression | Wave-2 cleanup and security recheck | **#5825** (defect on #3968) |

The full manifest keeps `required` at 10, so no subset of the wave can satisfy
`passed == required` and `not_run == 0`. That guard is unchanged by W2-01 and
W2-10 landing: it now bites on missing evidence rather than on missing code.

**W2-10 runs after cleanup, not with the other nine.** This is the one ordering
detail worth knowing, because it explains a report that otherwise looks odd. A
cleanup check running alongside the other checks would execute *before* the
deletions it describes, so the only thing it could assert is that you said
cleanup would work. Instead the harness runs the other nine, performs the
teardown, and then runs W2-10 against its own record of what it deleted — which
pairs, whether both key halves were present, and what the confirming consistent
read returned. That record is in `result.json` under `cleanup`, so a reviewer can
read the deletions rather than only W2-10's verdict.

Its live reads, though, happen *before* teardown. Those two facts together are
the whole of the ordering: **the harness captures, then tears down, then
verifies.** The capability and security surface is read while the fixture still
exists (`security_capture`), because a resource that has been deleted cannot
answer questions about how it behaves — and W2-10 then checks only what teardown
is supposed to have achieved, which is absence (`teardown_verification`). Nothing
you can put in either artifact makes W2-10 pass without the deletions having
actually succeeded, and a not-found from a deleted resource is never accepted
*as* the pass. A row record reporting `ok: true` because no rows were declared
does not satisfy it either; that case fails both W2-01 and W2-10, and the
published `cleanup_ok` follows the checks rather than the row record.

There is a shortcut worth naming because it is the natural place to reach: you
cannot answer a post-teardown request from a resource teardown removed. An
operator-supplied `"cleanup_succeeded": true`, or a `removals` entry for a name
that is not in the preflight's `creation_ledger`, **would simply be ignored** —
the first because it is a verdict where an observation belongs, the second
because a map whose keys you choose can only confirm the resources you chose to
mention, and the whole point of the ledger is that omitting a leaked resource
must not pass.

### Wave 4 (evaluation #3970)

Wave 4's manifest is registered in full — all ten IDs from #3970's acceptance
table — and S7 (#3966) implements the four whose subject is the dashboard it
builds. The other six consolidate criteria other stories own, and report
`not_run` naming that owner.

| ID | Acceptance IDs | Subject | Owner |
|---|---|---|---|
| W4-01 | Gate/regression | Wave-4 preflight: accepted waves 1–3, merged head, deployed frontend/gateway/worker revisions, green CI | operations |
| W4-02 | AC-F3 | Controls absent with the flag off, while loading and on backend error; only advertised capabilities offered | **S7 #3966** |
| W4-03 | AC-T1, AC-T2–T8, AC-S8 | Browser mid-run steer plus the W3-10 pivot and FIFO/retry/cap/SDK proof | S6 #3965 |
| W4-04 | AC-P1–P6 | Browser observes running→pause_requested→paused→running; truthful tool reason; spend-continues copy | **S7 #3966** |
| W4-05 | AC-A1–A12 | Abort cancel/confirm, terminal transition, aborted renderers and accounting | S4 #3963 with S5 #3964 |
| W4-06 | AC-S1–S7 | Deployed security matrix; every browser destination and body captured | S1 #3960 |
| W4-07 | Gate/regression | Live JSON matches `agentControl.ts` and `control_schemas.py` at each level | **S7 #3966** |
| W4-08 | Gate/regression | Measured polling lifecycle, backoff, stop conditions, distinct delivery states | **S7 #3966** |
| W4-09 | AC-F1, AC-F2 | Flag-off/flag-on runtime comparison with final code; live stats provenance | S5 #3964 |
| W4-10 | Gate/regression | Evidence index covering exactly all 37 acceptance IDs | operations |

As with wave 2, the full manifest keeps `required` at 10, so the four implemented
checks cannot satisfy `passed == required` and `not_run == 0` on their own. **A
complete wave-4 report is not reachable in this revision**, and that is the
correct state rather than a gap to work around: W4-03 needs steering to be
routable (S6 #3965 — the gateway's `SUPPORTED_ACTIONS` excludes `steer` today),
and W4-01 and W4-10 need waves 1–3 accepted, which is an operations act on a
deployed environment.

#### `browser_control_run` (W4-02, W4-04, W4-07, W4-08, wave 4)

The four implemented checks read a captured Playwright run rather than driving a
browser themselves — this harness is a Python HTTP prober, and giving it a browser
dependency would make every wave-1 run download Chromium.

Run the dedicated browser scenarios in **live** mode:

```sh
cd modules/gateway/frontend
npm install -D @playwright/test && npx playwright install chromium
CONTROL_E2E_LIVE=1 \
CONTROL_E2E_SESSION_FILE="$PRIVATE_FIXTURE_SESSION_JSON" \
CONTROL_E2E_DISABLED_URL="$FLAG_OFF_FIXTURE_URL" \
GATEWAY_URL="$FIXTURE_GATEWAY_URL" \
CONTROL_E2E_RUN_ID="$LIVE_RUN_ID" \
CONTROL_E2E_ABORT_RUN_ID="$ABORT_RUN_ID" \
  npx playwright test --config tests/e2e/agent-control.config.ts
```

`CONTROL_E2E_SESSION_FILE` is a private JSON file containing a real fixture user's
`access_token`, `id_token`, `expires_at_ms` and optional `refresh_token`. Keep it
outside the repository and evidence published to GitHub. Live mode never injects
the fabricated JWT used by the mocked browser tests. `CONTROL_E2E_DISABLED_URL`
must serve the same reviewed frontend bundle against a fixture with controls
disabled; live mode requires this check and does not skip it.

The scenario's default (mocked) mode is **not** valid evidence for these checks:
it stubs the gateway, so it proves the bundle's wiring and wording and nothing
about a worker. Only `CONTROL_E2E_LIVE=1` drives a real deployment.

The runner currently writes a standard Playwright report; it does **not** yet
produce the `browser_control_run` artifact below. A measured capture producer
remains required for live acceptance. Do not rename the Playwright report or
fill missing observations with configured constants: missing evidence must
remain `not_run`.

Every key below must come from browser observations; a source file cannot
establish it. `bundle_revision` is what ties the observations to a deployed asset —
a capture from a developer's dev server cannot answer for the deployment, and
W4-02 refuses one whose `gateway_url` differs from this config's.

| Key | Meaning |
|---|---|
| `bundle_revision` | Revision of the deployed frontend asset the browser loaded. |
| `gateway_url` | Deployment driven; must match the config's `gateway_url`. |
| `captured_at` | ISO-8601 instant, so the capture can be ordered against the revision. |
| `spec_digest` | Digest of the spec file that produced it, so a weakened spec is distinguishable. |
| `flag_off`, `flag_loading`, `flag_error` | Each an object with `control_nodes` and `command_requests` **counts**. Both must be 0. A boolean is rejected: it cannot distinguish "none" from "not measured". |
| `advertised_capabilities` | The capability object the gateway served. |
| `rendered_controls` | List of verbs the browser actually found. Must be a subset of the advertised ones. |
| `nonowner_submit_blocked`, `terminal_submit_blocked` | Observed at the request level, not inferred from a hidden button. |
| `phase_sequence` | Ordered phases rendered. Must contain running → pause_requested → paused → running. |
| `pause_copy_mentions_spend` | Whether the rendered pause copy says spend may continue. |
| `active_tool_reason` | The tool-activity text rendered. Must report an unknown count as unknown and never assert quiescence. |
| `steer_request`, `steer_status_sequence` | The steer request sent and the statuses rendered; a `delivered` with no preceding `pending` fails. |
| `poll_intervals_ms` | At least two **measured** intervals, each 1000–4000ms. A configured constant is not an observation. |
| `polled_while_hidden`, `polled_after_close`, `polled_after_terminal` | Must each be an observed `false`. |
| `backoff_intervals_ms` | At least two intervals observed while the endpoint was failing; must be non-decreasing. |
| `detail_refreshed_after_command` | Whether the invocation detail re-read after a command. |
| `request_destinations`, `request_bodies_contain_pod_address`, `request_bodies_contain_token` | Every destination the browser addressed, and whether any body carried pod coordinates. |
| `spoofed_identity_rejected` | Whether a spoofed identity was refused. |

Wave 3 remains unregistered and `--wave 3` is still refused outright. That is not
an oversight in wave 4: several wave-4 checks consolidate wave 3's criteria, so
wave 4 cannot be complete before wave 3 exists and is accepted.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Exit 2, `missing required fields` | Half-filled config | Fill every field; there are no defaults |
| Exit 2, `must be exactly true` | `fixture_isolated` is `"true"`, `1`, or absent | Use the JSON boolean. Do not work around it by enabling the flag more widely |
| Exit 2, `must not contain credentials` | A token pasted into the config | Move it to an env var and name the var under `identity_env` |
| Exit 3, account mismatch | Ambient credential is not the fixture account | Switch to `adp-embark1`. One of the two is wrong and it may be the credential |
| Exit 3, key schema | Wrong table, or the table changed | Confirm `event_id` HASH + `arrived_at` RANGE |
| Exit 4, several `not_run` | Artifacts not recorded yet, or `identity_env` vars unset | The message names the missing input per check |
| Exit 4, W1-02 `enumerate` | The three refusals are distinguishable | The 404 bodies must be byte-identical; a "not yours" body is an oracle |
| Exit 4, W1-08 503 wrong | Flag-off route returns 501 | The flag-off gateway is not actually flag-off, or precedence is wrong: flag-off 503 comes **after** authorization |
| Exit 5 | Cleanup incomplete | Remove the rows by exact key by hand, confirm with a consistent read, then rerun the wave |

## Related

* Story #3960 · evaluation #3967 · epic #3959
* `platform/scripts/agent-control-eval.py` — the harness
* `platform/scripts/tests/test_agent_control_eval.py` — its guard tests (run in CI)
* `.github/workflows/agent-control-ci.yml` — the CI job
* `modules/gateway/src/activity/control_schemas.py` — the response contract W1-09 checks against


For W2-08, `vocabulary_parity.suites` must include passing results for
`tests/activity/test_status_aborted.py`, `tests/test_status_vocabulary.py`,
`src/__tests__/utils/status.test.ts`, and
`src/__tests__/components/InvocationChain.test.tsx`.
For W2-09, export nonempty backend schema key lists for `response` (the root),
`today`, `daily`, `by_persona`, `active_runs`, `recent_failures`, `top_repos`, and
`spend` into `stats_schema_keys.levels`. Empty evidence cannot establish parity.
