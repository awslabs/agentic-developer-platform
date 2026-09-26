# Codex developer qualification — 2026-09-26

Stories #6195 and #6196 remain open. This is partial qualification, not deployed
ADP readiness or first-review acceptance of an arbitrary feature.

## Live inference observations

Official Codex SDK 0.155.1, Node 24, `openai.gpt-6-sol`, medium effort, through
ADP Gateway Responses. The gateway login was copied read-only into temporary
BG_CONFIG_DIR; no live configuration or token refresh occurred.

| Workload | Observed elapsed | Model calls | Tool calls | Outcome |
| --- | ---: | ---: | ---: | --- |
| Source fixture correction | 21.54 s | 6 | 5 | Passed detached check and fixture publication/completion |
| CommonJS retry-delay implementation | 45 s | 8 | 7 | Passed independent retry contract and fixture publication/completion |

The retry checker exercises defaults, jitter, caps, overflow, zero cases, invalid
arguments and random values, and input immutability. It executes outside the
candidate process in immutable image content, so changing repository tests or
calling process.exit cannot bypass its assertions. Image:
`sha256:18262fff825bd449fac2835c0d7273e909d25a06c8f8b65e3d9847e386040d42`.

Retry run summed model latency: 32.932 seconds; inclusive input 68,906 tokens and
output 3,301 tokens. Cached input is included in the input total. Actual monetary
cost has not been independently reconciled. Moto ledger prices are fixture data.

**Boundary:** model inference is live and billed to the user. Task identity,
DynamoDB/S3, admission/ledger and GitHub source/publication are fixtures. No actual
GitHub PR was created; fixture URLs/SHAs in evidence are not real provider
receipts. These observations were taken from the working tree during development,
not a deployed immutable image. They do not establish GitHub/GitLab acceptance,
remote controls, production latency, p95 or superiority to Claude.

## Regression evidence

- 86 TypeScript tests pass, including actual local OTLP export and collector-outage shutdown bounds.
- 152 focused gateway tests pass for admission, completion, publication, model transport, policy, receipts and finalization.
- 14 gateway/worker/official-SDK integration scenarios pass with fixture inference and local Docker validation; two explicitly live scenarios skipped in the default suite.
- Additional developer OTLP integration passes through the worker and real SDK.
- 33 signed worker-client tests pass, including trace headers and context cleanup.
- Broad worker suite: 2,477 pass, 12 fail, 2 skip. All 12 failures reproduce on pre-change commit `35c10df48` (2,475 pass, 12 fail, 1 skip). Failures concern redirect-test ordering, legacy distilled prompt size, legacy model enforcement, shellcheck and bootstrap fixtures.

The full gateway regression was interrupted to reconcile current main; no full
suite pass is claimed. After reconciliation, 169 focused worker tests, 94 gateway
model/admission/tool/finalization tests, and 15 gateway/worker/SDK scenarios pass.
All tests use run-isolated.py.

## Remaining release gates

Task-only metadata and the shared runtime command are now registered behind
explicit worker enablement (default off). The developer integration uses the
real registry, relocating only its packaged executable path.
Production workers also need a trusted validation executor; the current executor
requires local Docker. Full GitHub mention routing, GitLab provider qualification,
pause/resume, connected AWS/delegation, memory hooks and gateway memory integration
remain incomplete. No completed story count is claimed. Qualification must cover
real repository publication and independent requirements review before enabling
the developer for users.


## Follow-up qualification and discovered failures

A live run after the merge failed at final reporting despite completed code checks
and fixture publication. Another ran out of the fixture's legacy eight-turn budget
after publication. These are failures, not accepted changes. The live scenario now
explicitly admits the implemented 20-turn Codex budget, gives an exact report shape,
and retains failure evidence instead of writing evidence only after success.

A subsequent run completed in 31.07 seconds (6 model calls, 5 tools), but its report
cited instructions as evidence of implementation. Developer findings now require
host-known execution artifacts; inputs cannot stand in for execution evidence.
The stronger gate passed a live retry story in 32.48 seconds (7 model calls,
6 tools, 21.877 seconds summed model latency). Its final report cites the actual
implementation, check and publication artifacts. This still uses a fixture provider
and does not prove arbitrary feature quality or independent human acceptance.

The test images now default to UID/GID 65534, matching the executor's enforced
runtime user. Retry image: `sha256:66f98bf345199ea509277367682d0dfafd110f2898a82c9f05c905c63aeee6ca`.
Detached basic image: `sha256:65f2bd9245887d593a59f7d880d9bf4d2b5118c637138f59dafc15c44d666e1b`.

CI exposed a random UUID in pytest parameter IDs, causing divergent xdist
collection; the test now uses a stable foreign attempt identity (33 tests pass
under two xdist workers). The OTLP fixture also attempted export assertions after
its Docker-dependent story skipped; that fixture now skips before setup, while a
new report-only SDK export scenario runs without Docker. That CI-shaped suite
passes 9 scenarios and explicitly skips 9 requiring image/live configuration.
Gateway lint and formatting checks pass across all source and tests. The local
scanner test has 6 passes and 1 skip because Checkov is unavailable locally; CI
must confirm the image hardening fix. Runtime registration remains default off.


## Clean-commit live result

Commit `232b8092fc50758e959f52b2b761e06f79acce64` was tested with a clean
working tree and the non-root retry image above. The actual SDK/GPT-6 Sol medium
run completed in **38.97 seconds**: 8 model calls, 7 tool calls, 25.025 seconds
summed model latency, all 24 detached checks passed, fixture PR publication and
artifact-grounded final report verified. A local OTLP collector received **18
spans** (run, SDK turn, model, tool and completion) on **one trace**, **17
correlated log records**, and metrics. See `evidence-20260926/retry-clean-commit.json`
and `retry-clean-commit-otel.json`. This is repeatable live model qualification of
the committed local runtime; Task authority/storage/billing and GitHub provider
remain fixtures, not deployed ADP acceptance.

## One-hour checkpoint: 28-case repair and completion

Independent review exposed tiny-base overflow and huge-attempt/zero-base edge
cases missed by the original 24 checks. The detached checker now has 28 cases.
A live repair run passed those checks but failed before publication because the
bridge incorrectly applied the normalized request bound to the raw SDK envelope.
Commit `54f831b638774e7d037065e6a0dbea8f19bd4a17` separates the raw envelope
bound (at most 256 KiB) from the unchanged 63 KiB normalized gateway bound.

A clean-commit live GPT-6 Sol medium evaluation passed in **60.10 seconds**
(pytest wall time), with 13 model calls and 12 recorded tool effects. It detected
a failing acceptance check, repaired the code, passed all 28 cases, published
through the fixture GitHub adapter, and completed with artifact-grounded reporting.
This demonstrates repair-to-completion, not one-shot correctness. See
`evidence-20260926/retry-28-envelope-fix.json`. Task identity/storage and GitHub
publication remain fixtures; inference used the real ADP Gateway.

The latest TypeScript suite passes 90 tests. The broad gateway run had 2,840
passes, one refusal-wording assertion failure and five skips; the wording was
then corrected and the affected runtime suite passed all 33 tests. Full gateway
regression was not repeated after that wording correction. Latest-head CI was
still running at the checkpoint. Both foundation #6195 and developer #6196 remain
open: the release gates above are not satisfied by local fixture qualification.


## Recovery continuation: CI and validation cancellation

The recovered PR head had failing catalogue/probe expectations and an intermittent cancellation-finalization transaction conflict. Commit `0d181050b` corrects the catalogue expectations, refuses Codex profiles at the Anthropic-only probe worker before spend reservation, and retries rejected finalization transactions at most three times while rebuilding all authority fences. Caller-pinned versions and unknown write outcomes are not retried. A deterministic concurrent-receipt test fails before this fix and verifies one terminal event after retry; exhausted contention remains nonterminal.

Validation cancellation now propagates a host-owned stop event into the Docker executor. The executor kills the active validation container and verifies exit and removal. The worker waits for validation termination before finalization. Unconfirmed cleanup prevents finalization and downgrades stop-only settlement proof so capacity is not released on SDK-child exit alone. Cancelled checks cannot publish acceptance evidence, and uncertain validation cannot be replayed in that host. The CI worker test step includes these cancellation and workspace-binding contracts.

Validation performed with isolated configuration/token stores:

- Catalogue, selection and probe routes: **75 passed**.
- Task commands and routes, including injected finalization contention: **28 passed**.
- Worker validation tools, signed client and workspace binding: **56 passed, 2 skipped** (opt-in Docker cases).
- Real isolated Docker execution: **9 passed**, including cancellation after the container was observed running, verified removal, timeout, output bounds and source isolation. Image: `sha256:12ce6be82bed29b72198e4fdf3e51649aad17ab037d05975112b5fec0a2adf1d`.
- Combined gateway, worker and official SDK with fixture inference: **9 passed, 9 skipped** (opt-in image/live cases).
- Ruff checks and whitespace validation passed.

These changes do not qualify deployment, Kubernetes validation, pause/resume, GitHub mention invocation, GitLab publication or memory lifecycle/gateway integration. The disposable repository for live developer PR qualification remains unspecified. Both stories remain open and runtime registration remains off.
