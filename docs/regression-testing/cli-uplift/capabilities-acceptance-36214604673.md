# CLI-08 / #5621 acceptance crosswalk

The capability/doctor story has its required live E19 evidence and supporting
contract coverage. E19 passed in workflow [36214604673](https://github.com/aws-e/adp/actions/runs/36214604673),
evaluation `adp-e2e-20260926-031300-df33b4`. This is a story-specific result:
the mixed batch remains **failed** (four passed, one failed) because D05 failed
its stale-error assertion. Its original result is not rewritten.

| Criterion | Evidence and result |
|---|---|
| CLI-08-AC-01 | Capability CLI/server fixtures distinguish unsupported, disabled, denied, unknown readiness, missing dependency, old server and unreachable server. Tests verify preflight behavior, error wording/codes, authenticated tenant scope, malformed schemas, and deployment/identity/tenant cache separation and expiry. Passed. |
| CLI-08-AC-02 | Doctor transport tests capture GET-only bounded checks, forbid inference paths, verify own-scope lookup and redaction, and compare denied/absent responses. Server lookup tests verify tenant filtering and redacted metadata. Live E19 runs exactly `auth,api,budget,models,agents`, plus a real foreign request and absent request whose errors match. No remediation or model call is performed by E19. Passed. |
| CLI-08-AC-03 | Manifest tests compare every Python parser leaf and shell helper flags, dispatcher/help, request/output schema, owning tests, installed/downloadable artifacts and capability/mutation classifications. Command documentation explicitly labels shipped versus proposed forms. The EC2 preflight verified the served bundle against the expected release. Passed. |
| CLI-08-AC-04 | Fresh served `adp 1.0.0` reports gateway release `b1e266d97a1bf6e4a9c1805a02dd7482b3eafbe0` from `GATEWAY_RELEASE`; enabled `models.catalog.read` contrasts with intentionally disabled `logs.request.read`. Human output explains “switched off on this deployment.” Ordinary permission denial for `hierarchy.platform.write` differs from administrator permission in the same temporary HOME. Passed on disposable EC2. |

Independent post-run verification passed **124** capability, doctor, manifest,
server capability and request-lookup tests with all CLI configuration/token
stores isolated. Relevant doctor/common/manifest/server-capability source files
are byte-identical to the tested live gateway revision. Fixture tests establish
old/unreachable-server and malformed-response branches; those are not presented
as additional live deployments.

The foreign request ID was independently corroborated by a read-only usage-log
query as belonging to another identity before E19. The two request IDs and the
exact E19 result are retained in the companion JSON. The live scenario switches
between independent administrator and ordinary sessions in one temporary HOME,
with explicit private configuration stores, to test identity-cache isolation.
It never changes deployment flags or administrator permissions.

The report identifies account `000000000101`, region `us-east-1`, tenant
`adp-platform`, the gateway URL, harness commit, served revision, evaluation and
instance. E19 verifies temporary-home cleanup; the report records overall
cleanup complete. A fresh independent EC2 read confirmed
`i-00000000000000012` **terminated**. Worker revision is not applicable to this
no-inference diagnostic. The report artifact is
`cli-uplift-eval-36214604673-1`; its SHA-256 and retained case data are in the
companion JSON.

This closes the prior E19 live-evidence hold for #5621. It does not complete the
other CLI stories, turn D05 green, establish Task model execution, or claim full
epic acceptance. No issue state is changed by this evidence document.
