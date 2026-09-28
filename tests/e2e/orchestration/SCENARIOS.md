# ENGINE-Q2 delivery scenarios

Scenario implementation for #5157. #5158, #5133 and #5134 retain live acceptance.
Offline contract checks are not live qualification evidence.

`autonomous-delivery` creates two real-code issues and an accepted graph:
first story → deployed evaluation → human release → dependent story. Fixed tests
and the initial rounding defect are pinned in `scenarios/definitions.py`. The
first developer head intentionally uses Python's ties-to-even `round`, so the
half-cent test fails. An independent reviewer must record the actual correction,
the developer must repair it, and a current-head approval and required CI must
precede merge. The harness does not fabricate reviews or approve delivery PRs.

Fixture code lives below `modules/gateway/src/qualification/<qualification-id>`
so the gateway image contains it. Its pinned tests live below
`modules/gateway/tests/qualification/<qualification-id>` so gateway CI collects
them. This child changes harness code only; those real-code changes occur during
the separately approved Q3 run.

## Inputs and dispatch

Start with `config.q2.example.json` and `manifest.example.json`. These are
non-runnable examples: zero revision/account placeholders, an expired policy,
unresolved credential references and disabled native faults. Replace them in a
reviewed configuration; no example grants authorization to run.

Pin the reviewed harness code SHA first. Commit the configuration/manifest in a
subsequent commit. The harness verifies its Python files, reused UI/chat helpers
and shared E1 model against that code SHA, verifies configuration bytes against
checkout HEAD, and records both revisions. Do not edit the code during a run.

`secret_refs.owner`, `foreign`, `github` and `browser_session` name scoped
credentials; they contain no credential values. The browser session is the
existing Cognito token-restoration JSON used by the New UI fixture. Set
`E2E_CLOUDFRONT_URL` to the reviewed manifest origin. The resolver verifies the
authenticated owner, org GitHub registry and registered AWS connection. Runtime
and native access additionally verify the actual STS assumed role against that
registration, then sign Kubernetes authentication with that exact session.
Ambient kubeconfig exec plugins are not used.

Use `--preflight` before any approved `--run`. The manual workflow installs
Playwright/Chromium; ordinary harness CI blocks network sockets and has no AWS
SDK or credential discovery. Native probes also require `kubectl` on the live
runner. The harness refuses missing tools instead of substituting observations.

An E1 workflow dispatch invokes `execute_evaluation`, which reads the existing
flow/release and runs pinned tests inside the observed gateway image. It never
creates issues or another delivery flow. Its execution-specific concurrency key
allows it to run while the parent qualification waits. Supported E1 criteria are
`q2.deployed-code` and `q2.ui-api`, both required. Visual baseline or multi-role
claims outside the observed fixtures are refused.

## Fault and control coverage

All 24 criteria are mandatory and cannot be removed by configuration. A
mandatory `NOT_RUN` or `FAIL` prevents overall PASS.

| Case | Current adapter / boundary |
|---|---|
| Normal worker exit | Authenticated complete/exited invocation, durable continuation |
| Disposable worker loss | Exact flow → invocation → ScaledJob Job → Pod UID; single Pod DELETE with UID precondition |
| Tick restart | New interpreter exits after committed intent; a new interpreter resumes only at actual due time |
| Missed wake-up | Omit one isolated poll; verify unchanged durable revision before scoped recovery; shared scheduler race is NOT_RUN |
| Duplicate/out-of-order events | Replay actual stored settled observations through K1 |
| Competing launches | Actual K3 engine/direct admission against an already-held fixture claim; never admit a free lane |
| Timeout after success | Capture successful native effect receipt before response loss; require matching remote workflow and retained dispatch marker |
| Failed CI | Pinned rounding defect's actual required-check failure and blocked merge |
| Tenant denial | Authenticated different user/org receives 404 for the owner flow |
| Human refusal | Separate gate-only graph; actual human rejection and no successor dispatch |
| Stale image / failed deployment | Owned namespace, deny-all network policy and bounded sleep-only Deployment; fresh authenticated captures reach the deployed native runtime verifier |
| Revocation | Withdraw the accepted policy from a separate gate-only flow through the full amendment API; native admission must refuse stale authority, never revert to legacy access |
| Fan-out / repair allowance | Concurrent native reservations for real server-created fixture nodes exhaust one accepted flow allowance; additional and retry admissions must both refuse; unused holds are reconciled through the native service |
| Halt versus proven stop | Deployed capability check, separate owned issue/flow/worker, signed abort, exact generation acknowledgement, graph halt and independent invocation/Pod termination proof; unavailable abort remains NOT_RUN (#3963) |

Native faults require `native_faults`; worker loss requires `worker_loss` and
stop requires `halt_stop`. Before provisioning, the registered runtime revisions
and ownership are verified again. There is no shared deployment mutation,
operator credential revocation, direct ledger edit, queue purge or controller stop.

The runtime fixture starts from a verified old gateway digest. It runs only a
bounded sleep command, with no secrets, volumes, service account token, root
user or writable root filesystem. A healthy baseline must precede stale-image
validation and the failing readiness probe. The verifier runs deployed code over
an authenticated Deployment capture; it is explicitly a negative runtime-boundary
probe, not a fabricated successful D3 deployment receipt.

Allowance probes use three gate nodes that cannot launch work, and the actual
reservation service, including its concurrent Lua operation. They are admission
boundary checks, not claimed paid worker runs. `allowance_fixture_usd` must pin
exactly twice the actual per-run ceiling and fit inside `max_usd`; otherwise the
probe is NOT_RUN. The two holds and the denied fan-out/retry share the original
server flow binding. Only those undispatched holds are canceled. Actual delivery
review/repair is separately required by the primary scenario.

The complete inventory reserves at least 23 resource units, including bounded
Deployment ReplicaSet/Pod overlap and worker Job/Pod pairs. An unavailable stop
capability is reported before creating a paid worker. The stop fixture uses only
the known unspent remainder and one attempt, after primary workers have exited.
Final spend includes all fixture flows. Examples remain deliberately non-runnable.

## Evidence and cleanup

`report.json` and `summary.md` record fixed criterion ids, accepted definition and
manifest hashes, prerequisite and checkout revisions, actual PR/head/merge/runtime
bindings, policy and plan provenance, spend, inventory and interventions. Evidence
files carry source, observation timestamp and hash. Escaped paths, symlinks,
changed bytes, missing proof, simulations and unknown cost prevent PASS.

Every fault intent is written exclusively before mutation. Unknown outcomes are
not automatically repeated. The audit reads authenticated flow decisions,
complete invocation histories and paginated GitHub reviews/commits/timelines.
Manual resume/replan/halt overrides prevent an unattended PASS.
The current control API exposes a bounded worker acknowledgement journal, so it
cannot establish complete historical pause/resume/steer accounting. Collected
audit evidence is retained, but A6-6 stays NOT_RUN pending that #4539 integration.

Issues are closed during cleanup, not erased. Accepted flow rows are durable
audit state: no delete API exists, so the inventory explicitly retains them and
cleanup reports that reason. Worker cleanup never terminates another worker.
Code closure, deployed revision evidence and live acceptance are separate.

## Validation and acceptance

Run `python -m pytest tests/e2e/orchestration -q` for network-free harness checks.
From `modules/gateway`, run the Q2 native contract suites with real PostgreSQL:
`python -m pytest tests/orchestration/test_q2_native_contract.py tests/orchestration/test_q2_control_contract.py -q`.
The allowance contract test executes production Lua using fakeredis/lupa; it is
labeled non-live, as are the PostgreSQL fixture tests. Ordinary CI never performs
paid execution or resolves live credentials.

Report tests cover early credential/runtime failure and interruption. Evidence
resumption appends new files; it never overwrites a preceding capture. Cleanup
checks terminal invocation history, closes and retains provider audit records,
and deletes branches with an atomic expected-head lease. Namespace cleanup waits
for inventoried children; UID preconditions prevent deleting a replacement.

No Q3 run can pass until its prerequisite acceptance and complete approved
fixture manifest are available. Q3 remains 0/2; unavailable abort and durable
control history remain visible acceptance blockers rather than implementation
claims of successful live behavior.
