# Gateway Task tool receipt journal

Date: 2026-09-25. Component: `src/agentauth/task_tool_receipts.py`.

The journal stores tool claims and receipts in the existing `TASK_OPS` partition,
so existing content retention applies. It reuses Task tool authorization, protected
run-grant/policy fences and the current attempt. A deployment-owned one-to-one
mapping binds a gateway tool permission to its SDK function name; requests cannot
supply that mapping or alias a stronger permission to another function.

Claim requires one confirmed serial model call with the exact call ID, namespace,
name and canonical arguments from the same invocation/generation/attempt. Its
transaction fences both current Task authority and the exact model receipt read.
Arguments are retained as bounded canonical JSON text. A repeated claim returns
`created=False` and cannot authorize another execution. Claimed work is pending;
only its owning host may settle it, and a settled receipt is immutable.

Unknown outcomes remain unknown and forbid replay. Normal settlement after
revocation/cancellation is refused; late outcome reconciliation still requires a
separate stop-only path. This component does not itself execute tools, grant
repository/AWS access or charge external tool spend.

Twelve tests use real Moto DynamoDB transactions. They cover durable claims and
immutable settlement, changed arguments/tools/call IDs, unknown outcomes, foreign
owners and replaced attempts, live permission revocation, unconfirmed/foreign
model records, ambiguous catalogue mappings, and cancellation/model mutation
racing a claim. Existing tool-authority tests run alongside them.

The fixture inserts a confirmed tool-call model record explicitly. The current
Task Responses profile cannot yet create that record through its public model
contract. The HTTP integration is described below. Worker invocation, persona registration
and a distinct qualified tool transport profile remain required before
end-to-end tool execution is possible.

Reproduce from the repository root with the gateway development environment:

```sh
python3 modules/agent-factory/codex-harness/test/run-isolated.py -- env \
  AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 \
  /absolute/gateway-venv/bin/python -m pytest \
  modules/gateway/tests/agentauth/test_task_tool_receipts.py \
  modules/gateway/tests/agentauth/test_task_tool_authorization.py -q
```

All configuration/token stores are isolated. The separate current authentication
checkpoint remains unchanged; the earlier differing token baseline is preserved.

## Trusted-host HTTP claim and settlement

`POST /internal/v1/agent/task/tool-operation` now exposes closed, discriminated
claim/settle bodies through the existing IAM transport and authenticated Task
attempt boundary. Body attempt assertions must match the credential. Authority
is checked again before returning the result. No cleanup exception, repository,
endpoint, arbitrary owner or alternate identity can be inserted into a claim.

The route builds its catalogue from frozen Task tool grants. Its stable v1 SDK
function name is `adp_` followed by the first 56 SHA-256 hex characters of the
original gateway tool permission. Python and TypeScript share a fixed test vector.
This avoids delimiter collisions and the SDK name limit; reviewed implementations
still supply descriptions, argument schemas and routing. The mapping grants no
new permission and does not add a second persona registry.

Only a newly created claim returns an owner token to the trusted worker. Public
receipts, duplicate claims and settlement responses exclude that token and raw
arguments. A lost claim response therefore cannot become permission to reexecute.
The worker client has a run-bound `tool_operation` method; SDK frames do not carry
receipt ownership. TaskHost invocation wiring is described below.

Ten HTTP tests exercise actual parsing and DynamoDB receipts, while substituting
IAM and attempt authentication delivery. They cover the production journal
factory, stable mapping, identity/permission refusal, unverified transport, closed
request fields, settlement without a claim, ownership disclosure and replay.
All 45 route/journal/authorization tests, 30 worker-client tests and four shared
Task adapter tests pass. This is fixture evidence, not a deployed IAM test.

The model tool profile, durable model-history verification and persona
registration remain incomplete. No live tool operation
or agent story is enabled by the route alone.


## Worker claim, execution and settlement

TaskHost now claims a model-bound generic tool call before invoking the existing
host tool implementation. It verifies task, attempt, turn, call, permission and
canonical argument digest on the journal receipt. Only the newly returned owner
may execute and settle. The SDK receives confirmed canonical result/artifact
content only after settlement acknowledgement; ownership never crosses IPC.
Duplicate claims read receipts without invoking the effect. Lost settlement
acknowledgements produce an unknown child outcome while preserving any committed
receipt for a later read. Uncertain effects are never automatically replayed.

The shared child bridge preserves the canonical model turn ID, snapshots the
optional model-call binding and refuses owner tokens in host frames. The published
process schema documents generic tool frames; legacy investigator capability is
unchanged. TaskHost refuses unbound tools in the Responses path.

Five HTTP/worker scenarios cover success, lost claim, lost settlement, uncertain
effect and wrong receipt binding. They use the actual TaskHost, FastAPI route and
Moto-backed journal; IAM delivery, confirmed model-call insertion and the domain
effect remain fixtures. All 27 route/journal tests, 78 worker host tests, 59 shared
harness/IPC tests and 383 contract checks pass. TypeScript build and Python lint
pass. Live auth-store fingerprints remain unchanged from the tool-session
checkpoint. This increment does not qualify a tool-capable Task model or complete
a live persona story.


## Durable continuation verifier and separate tool wire contract

`TaskToolReceipts.verify_history` now reads canonical Task turns in order and
requires every earlier model operation to be confirmed in the same attempt.
Every model function call must have its matching immutable, confirmed tool receipt
and appear exactly once with its result in the submitted tool history. It checks
current tool authority, permission/function mapping, argument digest, canonical
turn and attempt, exact output text and the pinned SDK timing decoration. Missing
pairs, extra pairs, unknown receipts, changed arguments and foreign identities
are refused. The eventual model-claim caller must fence these reads against Task
version and authority changes; this method alone does not enable execution.

The separate `task-codex-sdk-serial-tools-v3` Python wire contract accepts one
reviewed namespace, serial calls and paired tool history. It bounds declarations,
arguments and usage and rejects alternate namespaces, model/endpoint overrides,
open root argument schemas, duplicate tools and parallel output calls. Structural
parsing does not authorize schemas or authenticate result text: frozen catalogue
comparison and the durable verifier remain mandatory. Existing report-only v2
request/result models still reject executable content.

The combined journal/route/wire suite passes 59 tests. New history tests use actual
DynamoDB reads and writes under Moto, with fixture canonical/model rows; they do
not prove deployed inference or model-claim integration. Live profile qualification,
frozen executable catalogue admission, route/model wiring and persona execution
remain outstanding.


## Gateway admission and model-path integration

The deployment-owned persona catalogue can now contain reviewed tool descriptors
(permission, capability and exact function schema). Admission freezes only tools
in the principal/persona tool-grant intersection; a missing descriptor, mismatched
stable function name or missing principal permission fails admission. Each tool's
capability must be present in every frozen capability layer. Protected bootstrap
size bounds include the descriptors. Resumption validates the frozen descriptors
without rereading mutable catalogue files.

Tool admission selects the distinct serial-tools-v3 model-evidence key; a report
or reviewer probe cannot certify it. The model route accepts the separate wire
contract. TaskModel compares its exact namespace with the protected catalogue,
requires the selected profile, and invokes the durable history verifier inside
the model claim before its Task-version/authority transaction. History refusal
happens before a budget reservation or provider send. The gateway-owned provider
adapter parses serial output and refuses undeclared names and repeated call IDs.
The schema exporter now publishes both Responses profiles.

Validation: 75 admission/harness/Responses tests pass; the model/model-binding/
harness group passes 47 tests. These groups overlap. All 383 contract checks pass.
The new model fixture exercises a successful first tool-profile model request,
refuses a caller-replaced declaration and refuses invented tool history on a later
canonical turn before any additional reservation or provider call. Inference and
budget sinks remain fixtures. The existing shared SDK Task entrypoint still does
not consume executable descriptors, and live tool-profile probing, a complete
Task tool continuation, persona registration and live story acceptance remain
outstanding. Gateway parsing alone is not persona enablement.


## Packaged SDK Task tool round trip

The shared Task entrypoint now consumes frozen descriptors, compiles their JSON
Schemas with pinned Ajv (no coercion, defaults, property removal or remote fetch),
and advertises the original schemas unchanged. It binds host tool IPC to the
canonical turn ID and sole confirmed model call. Confirmed result text survives
report-repair SDK sessions as tool history; the gateway still authenticates every
pair against durable receipts. The adapter retains only completed receipts and
bounds tool calls across repair sessions. Repository-bound tools still require a
repository admission adapter; this increment does not synthesize one.

The combined tests exposed two real worker gaps: the bootstrap/model IPC parser
still rejected tool metadata, and the dispatch gate admitted only the legacy
cyber SDK. Both are updated for the explicitly admitted Responses profile.
They also exposed an SDK approval requirement for write-capable MCP tools. The
session now sets per-tool `approval_mode = "approve"` only for its exact admitted
host catalogue. Gateway authority, capability checks, model-call identity and
claim/settlement still gate every effect. Tool annotations retain their write
semantics. Reference: https://developers.openai.com/codex/config-reference/
(`mcp_servers.<id>.tools.<name>.approval_mode`).

Seven combined gateway→worker→official SDK lifecycle scenarios now pass, including
a tool call, model continuation and report repair after a tool call. The latter
uses three model turns and exactly one tool effect. These tests use real protected
admission, canonical turns, model journal, authenticated attempt validation,
claim/settle route handlers, tool journal, durable artifacts and finalization.
Transport/IAM delivery, inference, budget sinks and the domain validation effect
are fixtures. The two tool scenarios also pass separately after removal of
local diagnostics. Eight relocated-package SDK scenarios, 61 shared harness/IPC
tests, 78 worker host tests and 383 contract checks pass; TypeScript build and
focused lint pass. Live authentication fingerprints remain unchanged.

No real repository story, live model qualification, deployment or persona rollout
is certified by these fixtures. Concrete repository/validation/AWS/delegation
brokers, completion policies, OTLP operational observability and live quality,
cost and latency acceptance remain outstanding.


## Executable persona completion gate

Shared sessions now require a host completion validator for every non-report
persona and require its explicit successful result before returning completion.
The validator receives no model-authored final answer. This prevents a configured
developer/reviewer persona from inheriting the generic report-only finish path.
The current Task entrypoint does not provide that validator, so those personas
remain unavailable there until their concrete completion adapter is wired.

`verifyRepositoryCompletion` implements shared developer/reviewer checks over
host-read durable evidence: the admitted repository/source, assigned PR/MR,
clean final commit, every immutable acceptance criterion, and every required
check with its specification/environment digests. Developer completion requires
an open, ready change. Reviewer completion additionally requires an approval and
zero unresolved findings for the final head, satisfied protections and a confirmed
merge of that reviewed head. Authority is checked before and after reading the
evidence. Models cannot supply these records through their final prose.

All 84 shared harness/IPC tests pass, including 23 completion tests covering stale,
missing, conflicting and foreign evidence, authority revocation and refusal before
inference when the host validator is missing. TypeScript builds successfully.
This is a shared verifier and session gate, not a complete developer/reviewer
implementation. Concrete durable evidence readers, repository tools, isolated
validation workspaces and live story qualification remain outstanding.


## Actual isolated validation execution

The new host-owned `DockerValidationExecutor` runs an admitted check against a
verified source archive or an expected clean Git commit. The local qualification
backend uses an immutable image ID, non-root UID, no network, a read-only root,
dropped capabilities, no-new-privileges, bounded memory/CPU/PIDs, and temporary
in-memory work/output directories. Only the verified source archive is mounted;
ADP/AWS/GitHub configuration and the Docker socket are absent from the container.
The Docker client also receives an isolated configuration environment. No mutable
image tags or implicit pulls are accepted by the executor.

Checks return commit/archive/check-specification/runtime-environment bindings,
confirmed process exit, bounded output and elapsed duration. Dirty or moved host
checkouts cannot produce a passing final-commit receipt. A named container is
removed even on failed creation/attach/timeout paths, and unconfirmed cleanup
raises an error. Actual excessive-output testing exposed a Docker attach
backpressure issue; stopping the attach reader before killing the container fixes
the teardown. UTF-8 replacement cannot expand output beyond its byte budget.

Seven actual local Docker tests pass with BusyBox image
`sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662`:
source/credential/network/capability isolation, nonzero exit, timeout, output flood,
invalid-UTF-8 output, changed-source/mutable-image refusal, and a real Git commit
with dirty-checkout refusal. Tests verify no retained validation containers and
unchanged source archives. Focused lint passes and live auth fingerprints are
unchanged. Run explicitly using `ADP_CODEX_VALIDATION_IMAGE` with the isolated test
wrapper; without a provisioned image the Docker integration tests skip.

This is real command execution, not a simulated validation result. The commands
are test fixtures, not generated application stories. The backend is not yet
registered as a Task tool or a source of durable completion records. Kubernetes
workers need a trusted executor service; mounting the Docker socket into the SDK
child is not part of this design. Existing legacy validation behavior is unchanged.
