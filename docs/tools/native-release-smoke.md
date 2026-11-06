# Native tools-platform release smoke

Native all-in qualification remains the default. An explicitly authorized dev
maintenance release may instead select the protected
[operational smoke policy](operational-release-smoke.md). That policy establishes
ordinary-user behavior and owned cleanup, with no native cost-bound or settlement
claim. It cannot be selected for production or an engine-correlated deployment.

The tools-platform release requires an original, protected `workflow_dispatch`
on `main`, attempt 1. A push can run source checks but cannot release this
component. The runner uses a dedicated fixture **access JWT** and the gateway's
existing native enrollment API; refresh-token login smoke is retired.

Before dispatch, the coordinator must have a real dedicated fixture identity,
tenant membership, consent, tool entitlements, an explicit approved search query
and a protected effect contract. The gateway must already implement native
all-in enrollment, the facade adapter, owned cleanup and authenticated run
status. Set the protected environment secret
`ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN` to that fixture's current access JWT.
Missing prerequisites cannot be filled with an owner login, a default query or
an unverified successful test record.

The coordinator dispatches the reviewed source and workflow revisions. Once the
original GitHub run ID is known, the owner uses the existing
`POST /api/v1/native-tests/approvals` API **outside CI**. Preserve its original
`run_started_at` and `expires_at`; delivery and Terraform apply do not reset the
300-second dispatch window or the 600-second cleanup window.

Create three private mode-0600 JSON files from that approval and the protected
fixture. `approval-reference.json` contains exactly these fields:

```json
{
  "version": 1,
  "run_id": "<original native run UUID>",
  "tenant_id": "<fixture tenant>",
  "approval_revision": "<original approval UUID>",
  "deployment_id": "<approved deployment>",
  "gateway_origin": "https://<approved public gateway host>",
  "source_revision": "<reviewed 40-hex source revision>",
  "definition_revision": "<actual 40-hex workflow revision>",
  "github_run_id": "<original GitHub run ID>",
  "github_run_attempt": "1"
}
```

`fixture.json` contains exactly `version: 1`, the same `run_id` and
`approval_revision`, the protected 64-hex `contract_sha256`, and
`arguments: {"query": "<explicit approved query>"}`. `approval-receipt.json`
contains exactly `run_id`, `approval_revision`, `scope: "all_in"`,
`cap_usd: "20"`, and the server's integer `run_started_at` and `expires_at`.
These files carry references to existing authority; writing them creates no
approval, consent, fixture designation or quote.

Use the once-write delivery helper with the existing authorized owner AWS
identity and the explicitly confirmed **runner evidence account**:

```bash
python3 -m modules.tools.platform.scripts.native_smoke_inputs publish \
  --approval-reference /private/approval-reference.json \
  --fixture /private/fixture.json \
  --approval-receipt /private/approval-receipt.json \
  --account-id '<confirmed runner evidence account>' \
  --region us-east-1
```

The helper confirms that account and performs one conditional encrypted PUT to
the existing private CI evidence namespace. It does not read a login or call an
approval API. Do not retry an unconfirmed publication or overwrite the object;
inspect the original publication. S3 receives no bearer credential.

The release job waits at most 120 seconds for that exact object, with at most
12 HEAD requests and one conditional GET. It verifies the checksum, version,
original run, source, definition, fixture and approval times. It uses the
existing runner IRSA before assuming the deployment role; no additional IAM
permission is required. Missing, expired or rebound inputs stop before Terraform
apply. The guard and release jobs have outer bounds of 2 and 12 minutes;
those bounds do not extend native approval or cleanup deadlines.

After the gateway and targets are READY, the runner exchanges its dedicated
fixture JWT and original producer proof for the native capability. It performs
live discovery, one exact search, one close of the original owned execution,
second discovery and authenticated original-run status. An unconfirmed search
is not repeated. Recovery can read the original execution for cleanup, within a
maximum of three status reads.

The release requires a successful search, stable discovery, confirmed owned
cleanup within the original deadline, and authenticated funding evidence.
Behavior may pass while funds remain held and accounting is pending; in that
case `governance_complete` remains false. A bounded quote or successful close
is not final settlement.

On success or failure, the job retains at most two private receipt files using
the existing CI S3 publisher: delivery metadata and native smoke evidence.
Neither contains the query, login, capability or provider response body. Each
file is bounded to 16 KiB; the archive is bounded to 64 KiB and its manifest to
8 KiB. Retention is 14 days. The uploader allows one identity read, two PUTs and
two HEADs, each with one transport attempt and a ten-second timeout. Private
login and fixture input files are removed in an always-run cleanup step.
Smoke failure prevents SSM and final workflow release publication. The final
workflow release artifact is also withheld if receipt upload fails; SSM metadata
may already reflect the successful smoke in that case and explicitly retains
`qualification_status: smoke_passed_evidence_pending`. Only a confirmed private
retention receipt bound to the native evidence digest allows the final workflow
artifact to become `selected_smoke_verified_evidence_retained` with its immutable
S3 manifest reference. This qualifier covers the selected smoke, not all tools or
final accounting.

This smoke validates the selected stateless search and release wiring. It does
not establish coverage for other tools, updated CLI behavior, or settled
all-tool governance; those require their own explicit fixtures and live tests.
