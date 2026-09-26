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

The full gateway regression run remains in progress; its final outcome must be
recorded before claiming that suite passes. All tests use run-isolated.py.

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
