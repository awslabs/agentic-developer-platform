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
    "pause_expiry": "artifacts/pause_expiry.json"
  },

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

### Observation artifacts

Seven of the ten checks need observations the harness cannot make itself — a
`kubectl exec` from a probe pod, a token that has actually expired, two fixture
runs compared. Those are recorded by the operator as small JSON files and
referenced from `artifacts` by path, relative to the config file.

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

## Wave 2 is incomplete on purpose

`--wave 2` registers all ten checks from evaluation #3968. S3 provides W2-02
and S5 provides W2-06 through W2-09. With complete passing fixture evidence,
these five pass and the five pending checks report `not_run` with their owner.
The command exits nonzero until the full wave is implemented and accepted.
Missing artifacts also report `not_run`; implemented checks can fail when the
observed deployment disagrees with the contract.

## Cleanup

Always runs, including on the failure path — that is when a fixture is most
likely to be left with a live listener. Raw evidence is written first.

Cleanup is bounded to the exact `(event_id, arrived_at)` pairs in
`cleanup_items`. There is no scan, no query, no prefix and no wildcard, so an
item the harness was not told about is unreachable by construction rather than
by care. A pair missing either half is refused rather than guessed at, because a
delete keyed on `event_id` alone could match an unrelated item. Absence is
confirmed with a consistent read, since an eventually-consistent one can report
an item gone before it is.

Cleanup failure is exit 5 and `cleanup_ok: false`, which fails the gate even
with ten passing checks. **Never** purge a shared queue or delete ordinary
objects to make cleanup succeed.

Fixture workloads and probe pods are yours to remove — the harness deletes only
the synthetic rows it was given.

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
  "cleanup_ok": true
}
```

`checks` is an **object keyed by check ID**, which is what makes
`.checks[$id]` in the `check()` helper work. Every entry carries a nonempty
`evidence` list; a status string without evidence is not sufficient.

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
table. S3 (#3962) implements W2-02 and S5 (#3964) implements W2-06 through
W2-09; five checks remain pending.

| ID | Acceptance IDs | Subject | Owner |
|---|---|---|---|
| W2-01 | Gate/regression | Wave-2 preflight consolidation | S2 #3961 |
| W2-02 | AC-T7 | Neutral adapter contract suite | **S3 #3962** |
| W2-03 | AC-P1 | Pause admission control | S2 #3961 |
| W2-04 | AC-P2 | Resume semantics | S2 #3961 |
| W2-05 | AC-P3, AC-P5, AC-P6 | Auto-resume and watchdog behaviour | S2 #3961 |
| W2-06 | AC-A3, AC-A9 | Aborted row is terminal with a completion time, filterable, and reached that state via ADP finalization rather than a native interrupt | **S5** |
| W2-07 | AC-A10 | Each aborted row counted exactly once; no other outcome reclassified | **S5** |
| W2-08 | AC-A11, AC-A12 | Writer allowlist and reader vocabulary in parity across both deployed images; unknown statuses rejected before the write | **S5** |
| W2-09 | AC-A10 | The live run-stats response carries the aborted counter at every level | **S5** |
| W2-10 | Gate/regression | Wave-2 cleanup and security recheck | evaluation #3968 |

The five pending checks report **NOT RUN**. The full manifest keeps `required`
at 10, so partial implementation cannot satisfy `passed == required` and
`not_run == 0`. Evidence for S3 and S5 alone therefore cannot accept Wave 2.

So `--wave 2` exiting nonzero today is the correct result, not a defect to work
around. Waves 3 and 4 are still unregistered and still refused outright.

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
