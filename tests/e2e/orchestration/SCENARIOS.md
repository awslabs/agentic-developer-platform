# ENGINE-Q2 delivery scenarios

Implementation checkpoint for #5157. The code is not yet ready for qualification
or code closure: the unsupported fixture cases and remaining checklist below are
explicit. #5158, #5133 and #5134 retain live acceptance.

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
| Stale image / failed deployment | NOT_RUN: no inventoried disposable rollout target; owners #5152 / #5151 |
| Revocation | NOT_RUN: no disposable authority fixture; owner #5128 |
| Fan-out / repair allowance | NOT_RUN: public cost omits in-flight reservations; isolated allowance proof missing; owner #5128 |
| Halt versus proven stop | NOT_RUN: no separate halt-control worker; owner #3963 |

The last six cases are implementation/integration gaps, not successful tests or
claims that the platform supports their safe injection. Do not approve this
checkpoint as complete solely because its offline tests pass. Shared deployment
changes, revoking the operator connection, direct budget-ledger edits, shared
controller stops and queue purges are not substitutes for disposable fixtures.

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

## Remaining completion checklist

- Connect the six unavailable cases to reviewed disposable fixture contracts.
- Finish terminal-worker proof and safe cleanup of worker-created branches;
  PR/branch intents are now recorded before dispatch and reconciled from bindings.
- Complete visual/role fixtures only where the accepted E1 specification
  requires them; current owner/release binding and adversarial report checks exist.
- Validate all full-report paths, credential/clock failures and interruption
  accounting; preserve artifacts when an early prerequisite fails.
- Run the affected integrated regression once, perform Root contributor review,
  open one ready PR, pass required CI and merge normally.
- Execute no Q3 run until its prerequisite acceptance and complete approved
  fixture manifest are available. Q3 remains 0/2 at this checkpoint.
