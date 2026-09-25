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
