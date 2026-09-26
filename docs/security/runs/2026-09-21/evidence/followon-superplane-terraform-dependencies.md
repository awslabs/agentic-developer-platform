# Executor Terraform 1.9.8 security rebuild — #6124

The executor's unchanged Terraform 1.9.8 binary accounted for all eight Critical
findings in the preceding exact executor image scan. Its separate raw binary
scan contains 89 findings: the full reports are retained losslessly as
`followon-superplane-terraform-baseline-grype.json.gz` and
`followon-superplane-terraform-rebuilt-grype.json.gz`. These are fresh unfiltered
observations, separate from the original frozen Superplane image denominator.

The workspace module explicitly requires Terraform `= 1.9.8`, and its plan/state
consumers bind to that format. The Dockerfile therefore builds source commit
`e044e569c5bc81f82e9a4d7891f37c6fbb0a8a10` (upstream v1.9.8) with pinned Go 1.26.8
and an exact dependency/source patch. No floating dependency update runs in the
build. Compilation has no network, uses readonly modules, and verifies go.mod /
go.sum are unchanged. Runtime retains the original license and a downstream
NOTICE. The binary reports 1.9.8 to retain saved-plan compatibility; it is not
represented as an unmodified upstream release.

The patch retains HCL 2.20.0 and cty 1.14.4. It updates vulnerable crypto,
networking, gRPC, OpenTelemetry, archive/module download, NTLM, AWS S3/eventstream,
and related dependency selections. The source compatibility changes are explicit:

- S3 AssumeRole's new SDK slice representation receives exactly the one existing
  configured role, for both legacy and nested configuration forms.
- OpenTelemetry's removed unary/stream interceptors become supported gRPC stats
  handlers, preserving request tracing through the modern SDK.
- Diagnostic formatting uses the raw invalid target string and a literal gRPC
  error message; Go 1.26 vet previously rejected the old formatting calls.
- Test-only updates print literal diffs and compare the new telemetry scope /
  attribute-set representation without dropping field assertions.

Validation:

- 116 upstream tests/subtests pass across planfile, statefile, local backend and
  RPC, including client/server trace relationships. Go vet remains enabled.
- 35 upstream inline/nested AssumeRole tests/subtests pass using synthetic local
  HTTP mocks. No live cloud credentials, endpoints, or state are used.
- 22 existing executor build-boundary tests pass through the required isolated
  CLI-store wrapper.
- Initial compiler/vet and telemetry-structure test failures remain in task-local
  logs; all required compatibility corrections are visible in security.patch.
- The isolated nonroot lifecycle fixture applies a baseline-generated plan with
  the rebuild, then applies a rebuild-generated plan with the baseline; both
  report 1.9.8. State lineage/serial, state move, no-change plan exit code, and
  destroy cleanup pass using only builtin terraform_data and disposable local
  state. Fixture and exact binary hashes are retained in the adjacent lifecycle
  script/receipt. No shared or production Terraform state is opened.
- Exact final binary SHA-256:
  `0f4c860aaf922997867337b2ee97029f94ae8c2e1fee40dc442e47e706e35245`.
- Frozen Grype DB built 2026-09-25T06:31:49Z, with updates disabled and no ignores:
  zero raw matches for that exact final binary, versus 89 for the exact baseline
  SHA-256 `7386e89a97d0f24024955acc79ccf693b75b97f7c6383cb9d966e7d59aa5b223`.

The locally built root module metadata is `(devel)`, so scanner absence does not
establish applicability for every Terraform advisory. The original source version
is explicitly retained. Remote provider/backend runtime behavior, remaining
executor AWS CLI/Python/OS findings, and complete image-family reconciliation
remain separate acceptance requirements. No publication or rollout is implied.

The combined executor image (both kubectl and Terraform rebuilds) passes the
nonroot, readonly, offline runtime checks, including both exact binary hashes,
imports/pip check, kubectl dry runs, and paid-worker refusal/service idle when
authority is absent. Docker-save config bytes independently verify config
`sha256:4aad4f2b9d8ba8b5f52e19e91a280bfadc698eda31b2349f6151478a812b5526`.
Its unfiltered scan retains 218 findings: 62 High, 94 Medium, 16 Low and
46 Negligible; no Critical. All remaining raw findings are retained losslessly
in the adjacent compressed report. The preceding executor image had 307 raw
findings, including eight Critical. These local observations do not close #6124.
