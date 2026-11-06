# Core provider observation contract

`provider_observer.py` collects bounded read-only facts for the ten Web Search,
Code Interpreter and Browser operations in the client matrix. It is separate
from the client, tools facade, provider dispatch paths and accounting authority.
It does not decide whether a matrix cell passes.

This module does not authorize dispatch. A working observer, a price estimate,
`authority_confirmed`, and a runner `live` flag are not the #8078 server-side
admission authority. The runner still needs that genuine gate before any setup
or tool/model dispatch, including local execution. Nothing in this change wires
or enables paid runner execution.

The provider contract includes the reviewed cleanup changes: public start
`ttl_seconds` set to at most 300, immutable execution deadlines, and the browser
start record's second copy of the provider session identity. Deploy compatible
components and verify the runtime window before using this observer. Source and
release receipt hashes in its private binding are provenance references; this
module does not independently verify the running images.

## Facts and limitations

Each fact has its own `status`: `verified`, `pending`, `unsupported` or `refused`.
The result always contains `acceptance: "not-evaluated"`.
Reports from injected readers additionally carry `transport_kind:
"injected-reader"` and `offline_only: true` and cannot be adopted as live
evidence. `SDKReader` reports `transport_kind: "sdk-reader"` and
`offline_only: false`. This identifies the transport implementation, not a
cryptographic attestation or a claim that release/runtime/admission/accounting
checks passed; the combined verifier still owns those checks.

| Fact | Source and meaning |
|---|---|
| `correlation` | Exact provider-owned ledger key, execution/request/receipt identity, input digest and root/parent relationships. |
| `durable_outcome` | Recorded state and, where available, result status/provider task ID. A verified recorded failure is still a failure. `scope` distinguishes session admission, parent operation, parent cleanup and operation outcome. |
| `provider_artifact` | Independently hashed stored payload bytes, with an optional expected digest/length and bounded chunk comparison. It does not hash the facade's enclosing result envelope. |
| `session_ttl` | Direct AWS Get-session identity, Code Interpreter session name, creation time and configured lifetime against the admitted deadline. |
| `termination` | Direct AWS Get-session response with the bound IDs and `TERMINATED`, observed within the controller's declared cleanup window. `READY`, not-found, errors and unknown IDs do not prove termination. |

Web Search has no session proof; its session facts are `unsupported`. Interpreter
result reads the parent execute row; close reads the root start/session row.
Neither is a newly recorded invocation audit. An absent artifact is unsupported
unless the binding requires it, in which case it remains pending.

Exact upstream request count, actual cost, global session count, per-invocation
audit, and complete facade artifact proof remain explicitly unsupported. The
observer never copies a fixture's expected count, quote or cleanup flag into an
observation. Its `read_attempt_count` counts its own SDK attempts, including
failed reads; `read_count` counts completed reads retained in provenance. Neither
is a provider dispatch or billing count.

The matrix verifier must compose these facts with the separate provider-audit
and accounting contracts, native/CLI call evidence, and runtime release window.
The existing `client_matrix.verify_readback` aggregate schema is intentionally
not impersonated. There is no fabricated universal `provider-control-plane`
source. Unsupported families report no capability and must not be dispatched
when their required evidence is unavailable.

## Private controller binding

The fixture controller supplies the exact account, region and SHA-256 of a
private JSON manifest out of band. The manifest must be a regular file owned by
the process user, inaccessible to group/other users, at most 1 MiB and opened
without following symlinks. No default/administrator credential fallback exists:
the factory requires either an explicit dedicated observer session or an
injected read function for tests.

The top-level fields are exactly:

```json
{
  "version": 1,
  "deployment": {
    "account": "123456789012",
    "region": "us-east-1",
    "observer_role_arn": "arn:aws:iam::123456789012:role/fixture-observer",
    "shared_table_arn": "arn:aws:dynamodb:us-east-1:123456789012:table/fixture-shared-operations",
    "browser_table_arn": "arn:aws:dynamodb:us-east-1:123456789012:table/fixture-browser-operations",
    "interpreter_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:code-interpreter-custom/fixture_interpreter",
    "browser_identifier": "aws.browser.v1",
    "provider_source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "release_receipt_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "authority": {
    "principal": {
      "tenant_id": "fixture-tenant",
      "user_id": "fixture-user",
      "team_id": "fixture-team"
    },
    "run_id": null,
    "workload_id": null
  },
  "limits": {
    "max_reads": 64,
    "max_polls": 12,
    "poll_seconds": 2,
    "timeout_seconds": 60,
    "max_item_bytes": 200000
  },
  "operations": []
}
```

These identities are fictional and are not deployment targets. Supply between
one and 90 real operation bindings privately. Account, region, table names and
provider resource selections come from approved deployment evidence, not tool
responses. Principal/run/workload identity comes from the authorized fixture.
Null run/workload values must be intentional; a run requires both fields.

Each operation binding has these exact fields (explicit nulls are required):

- `client`: `cli`, `claude-code` or `codex`.
- `operation`: one of the ten core public operation names.
- `operation_ref`, `execution_id`: the controller's captured client invocation
  and owned execution UUIDs. They select the prebound cell, not a discovery query.
- `provider_request_id`, `provider_receipt_id`: expected provider row identity.
  The request equals the outer reference except for interpreter result/close,
  which explicitly bind the parent execute/root start identity.
- `arguments`: exact resolved provider arguments, including injected execution
  and request IDs; interpreter close has no provider `request_id` argument.
  These are not just the unresolved public `$owned.*` fixture arguments.
- `root`: null for search; otherwise exactly `request_id`, `receipt_id`,
  `arguments`, `deadline_at` (integer Unix seconds), `ttl_seconds` (1–300).
  `arguments` is the original provider start payload: execution/request IDs and,
  for browser, the start URL. This records owned setup lineage.
- `parent`: null except interpreter result; then exactly
  `operation: "code_interpreter___execute"`, `request_id`, `receipt_id` and the
  original execute `arguments`, including code/language/session/execution IDs.
- `artifact`: null or exactly `scope: "provider-payload"`, `sha256`,
  `byte_length`, `offset`, `length`. Offset is within the payload and requested
  length is 1–8192. The independently read bytes must match these expected
  metadata; a facade-envelope digest cannot be substituted.
- `cleanup`: null for search; otherwise exactly `trigger` (`public-close` or
  `deadline`), `requested_at` (timezone-bearing timestamp), `max_seconds`
  (1–600). A deadline trigger must equal the immutable root deadline. The
  controller owns public close/recovery. The observer never performs cleanup.

Dynamic execution/reference values are allocated during invocation. The trusted
controller resolves and pins their private binding before observation; do not
pretend they can all be predeclared in a static deployment manifest. Check the
adapter's capabilities and all other required evidence before starting any paid
cell. Bind input/lineage from the controller's owned invocation records, not
solely from response claims. Keep the private manifest after interruption for
an independently reviewed recovery.

## Storage and identity checks

Canonical encoding is UTF-8 JSON with sorted keys, compact separators and
`ensure_ascii=False`. Let `P = [tenant_id,user_id,team_id,run_id,workload_id]`,
`E` be the execution UUID and `R` the provider request UUID:

| Provider table | Partition (`event_id`) | Sort key (`arrived_at`) |
|---|---|---|
| Shared search | `GATEWAY_SEARCH#` + SHA-256 of canonical `[P,R]` | `OP` |
| Shared interpreter | `GATEWAY_CODE#` + SHA-256 of canonical `P+[E]` | `CODE_SESSION`, `CODE_OP#R` |
| Browser | `GATEWAY_BROWSER#` + SHA-256 of canonical `P+[E]` | `SESSION`, `OP#R`, `ARTIFACT#R` |

The observer makes strongly consistent exact GetItem calls. It verifies every
returned key; uses a projection that excludes `provider_token`, assertions and
credential fields; and rejects extra returned fields and oversized items. Search
result bytes are limited to 64000, interpreter result bytes to 24000, decoded
files to 8192, and browser artifacts to 128000. Payload bytes remain in memory;
the result contains hashes/lengths, not private code, queries, captures or tokens.

Browser SESSION and start OP identities must agree when both exist. The
cleanup-compatible start-owned copy may supply a missing SESSION provider ID
only with matching root request, receipt, digest, generation, dispatch fence and
immutable deadline. This produces `identity_source: "start-ledger"`; it does not
write a repaired session row. The Code Interpreter AWS session name must be
`adp-` followed by the independently derived owned session handle.

An initial STS read must match the selected account and assumed observer role.
SDKReader freezes explicitly supplied credentials, disables configured endpoint
URLs, uses one-second connection/two-second read timeouts and one total SDK
attempt. Total calls, polling iterations, delay, item bytes and elapsed time have
independent limits. A completed read after the deadline cannot produce verified
facts. Timestamp comparison allows one second for provider timestamp precision;
no stop timestamp is invented when the provider API does not return one.

## Permissions and integration

Provision a dedicated role with `dynamodb:GetItem` on the two exact approved
provider tables and only `bedrock-agentcore:GetBrowserSession` and
`bedrock-agentcore:GetCodeInterpreterSession` for the selected environment. Use
validated resource/region restrictions. Add exact `dynamodb:LeadingKeys` when
execution-bound role session policies can be provisioned. This module does not
assume a role, change IAM, or claim that caller-identity validation inventories a
role's permissions. Its method allowlist still refuses every write, Scan/Query,
Start/Stop, Lambda invoke and provider invocation method.

A controller can construct the adapter without touching the active runner:

```python
from tests.e2e.tools.provider_observer import from_manifest

core = from_manifest(
    controller_private_manifest_path,
    expected_sha256=controller_manifest_sha256,
    account=approved_account,
    region=approved_region,
    session=explicit_dedicated_read_only_session,
)
facts = core.observe(
    operation_ref=owned_operation_ref,
    execution_id=owned_execution_id,
    operation=operation,
    client=client_name,
    timeout=30,
)
# Hand this fact set to the combined evidence verifier. It is not an acceptance.
```

Store fact sets as private evidence until the publication layer applies its
schema/redaction policy. Resource/correlation metadata can be sensitive even
though credentials and payload bytes are excluded. Never provide personal or
administrator refresh tokens in CI. Offline tests inject a reader or use
botocore Stubber; they perform no cloud, model or provider calls.

## Installed-client recovery binding

The harness retains the session start's `$owned.operation_ref` and receipt for
execution close/recovery. When a setup child returns an artifact, it also retains
`$owned.artifact_operation_ref` and `artifact_receipt_id` alongside that artifact's
digest and size. Artifact-read fixtures must use the artifact reference; the
start reference cannot stand in for a child that produced different bytes.

The protected Linux harness terminates its detached auth proxies before leaving
an installed-client scope, including interrupted runs and reopened recovery
homes. It verifies the private identity against the installed CLI's target and
process birth-time checks, then signals a pidfd for that exact process. Missing
pidfd support or mismatched live identity refuses cleanup verification. It does
not signal other proxies or process groups. Saved tool operations remain available
for recovery after the test-user token is removed.

Protected evidence consumption requires every maintained authorization case,
including Browser permissions, with exact case names, expected/observed boolean
decisions and the private deployment selector binding. An anonymous pass, a
duplicate/missing case, another workflow attempt, or another target is rejected.
The retained reader binds the harness to `GITHUB_SHA`, the release window and
façade receipt to the independently supplied `SOURCE_REVISION`, and the gateway
receipt to `GATEWAY_REVISION`; these revisions may differ. Artifact content cannot
select an expected source. Publishing preserves an exact public operation name
only in its matching, allowlisted provider assertion cell. Credential values and
other free text still pass through the secret redactor before row checksums are
computed.
These harness checks do not enable paid execution; #8078 admission remains required.
