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
contract. No HTTP claim/settlement route, worker invocation or persona registration
is enabled by this increment. Those integrations and a distinct qualified tool
transport profile remain required before end-to-end tool claims are possible.

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
